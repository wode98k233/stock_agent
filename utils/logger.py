"""
选股雷达 - 结构化日志系统（重构版）

核心设计：
  1. 请求级 context（req_id）贯穿所有日志
  2. 结构化字段：tag + req_id + message + kv 数据
  3. 生命周期事件：step_start / step_end / tool_call / tool_result
  4. 全量记录：工具输入输出完整写入日志（DEBUG 级别）
  5. 摘要记录：INFO 级别自动摘要，不截断关键信息
  6. 自动追踪：源文件路径 + 函数名 + 行号（stacklevel 机制）
  7. 毫秒精度：时间戳格式 YYYY-MM-DD HH:MM:SS.ms
  8. 统一接口：RadarLogger 为唯一日志类型，消除 isinstance 分支

v2 优化：
  - RequestContext 改用 contextvars，支持并发/异步安全
  - error() 消除与 _log() 的重复代码，exc_info 统一透传
  - DEBUG 级别昂贵操作（json.dumps / _summarize）增加 isEnabledFor 守卫
  - _log 增加 level 预检查，跳过无需执行的字符串格式化
  - get_child_logger 自动继承 _RelativePathFilter
  - 新增 close_logger() 释放 handler 资源
  - 修复 _prefix 空 req_id 尾部空格
  - log_phase 增加阶段耗时记录
"""
import logging
import uuid
import json
import time
import os
import contextvars
from datetime import datetime
from contextlib import contextmanager
from typing import Optional
from config import Config
from utils.app_paths import get_logs_dir
from utils.cache import get_db

os.makedirs(get_logs_dir(), exist_ok=True)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 并发安全的请求上下文存储（替代类变量 _current）──
_current_ctx: contextvars.ContextVar[Optional['RequestContext']] = contextvars.ContextVar(
    '_current_ctx', default=None
)


class _RelativePathFilter(logging.Filter):
    def filter(self, record):
        try:
            pathname = record.pathname
            if pathname.startswith(_PROJECT_ROOT):
                record.relpath = os.path.relpath(pathname, _PROJECT_ROOT).replace('\\', '/')
            else:
                record.relpath = os.path.basename(pathname)
        except Exception:
            record.relpath = record.filename
        return True


_LOG_FORMAT = '%(asctime)s.%(msecs)03d │ %(levelname)-7s │ %(relpath)s:%(lineno)d │ %(funcName)s │ %(message)s'
_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# ── level 名称 → 数值映射，避免每次 getattr ──
_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class RequestContext:
    def __init__(self, req_id: str, logger: logging.Logger):
        self.req_id = req_id
        self.logger = logger
        self.start_time = time.time()
        self.trace_recorder = None
        self.metrics = {
            "llm_calls": 0,
            "llm_tokens_in": 0,
            "llm_tokens_out": 0,
            "tool_calls": 0,
            "steps_total": 0,
            "steps_success": 0,
            "steps_failed": 0,
            "retries": 0,
        }

    @classmethod
    def current(cls) -> Optional['RequestContext']:
        return _current_ctx.get()

    @classmethod
    def set_current(cls, ctx: Optional['RequestContext']):
        _current_ctx.set(ctx)

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def record_llm(self, tokens_in: int, tokens_out: int):
        self.metrics["llm_calls"] += 1
        self.metrics["llm_tokens_in"] += tokens_in
        self.metrics["llm_tokens_out"] += tokens_out

    def record_tool(self):
        self.metrics["tool_calls"] += 1

    def record_step(self, success: bool):
        self.metrics["steps_total"] += 1
        if success:
            self.metrics["steps_success"] += 1
        else:
            self.metrics["steps_failed"] += 1

    def record_retry(self):
        self.metrics["retries"] += 1


class RadarLogger:
    """
    结构化日志包装器 —— 全局唯一日志类型。

    设计原则：
    - 所有模块统一使用 RadarLogger，不再有 isinstance 分支
    - tag 默认 "U"，兼容单参数调用：logger.info("msg") 等价于 logger.info("U", "msg")
    - 通过 stacklevel=3 自动追踪真实调用者的文件/函数/行号
    """

    def __init__(self, logger: logging.Logger, req_id: str = ""):
        self._logger = logger
        self._req_id = req_id[:8] if req_id else ""

    def _prefix(self, tag: str) -> str:
        if self._req_id:
            return f"[{tag}] req={self._req_id}"
        return f"[{tag}]"

    @staticmethod
    def _parse_args(args):
        if len(args) == 0:
            return "U", ""
        if len(args) == 1:
            return "U", args[0]
        return args[0], args[1]

    def _log(self, level: str, *args, stacklevel: int = 3, exc_info=None, **kv):
        # 预检查：level 不够时跳过所有格式化开销
        level_no = _LEVEL_MAP.get(level, logging.DEBUG)
        if not self._logger.isEnabledFor(level_no):
            return

        tag, message = self._parse_args(args)
        parts = [self._prefix(tag), message]
        if kv:
            kv_str = " │ ".join(f"{k}={v}" for k, v in kv.items())
            parts.append(f"│ {kv_str}")
        log_method = getattr(self._logger, level)
        log_method(" ".join(parts), stacklevel=stacklevel, exc_info=exc_info)

    def info(self, *args, **kv):
        self._log("info", *args, **kv)

    def debug(self, *args, **kv):
        self._log("debug", *args, **kv)

    def warn(self, *args, **kv):
        self._log("warning", *args, **kv)

    def warning(self, *args, **kv):
        self._log("warning", *args, **kv)

    def error(self, *args, exc_info=None, **kv):
        # 复用 _log，消除重复代码；exc_info 统一透传
        self._log("error", *args, exc_info=exc_info, **kv)

    def phase(self, name: str):
        self._log("info", "R", f"══ 阶段: {name} ══")

    def step_start(self, step_num: int, skill: str, purpose: str, instruction: str):
        self._log("info", "E", f"┌─ Step {step_num} 开始: [{skill}] {purpose}")
        self._log("debug", "E", f"│ 指令: {instruction}")

    def step_end(self, step_num: int, success: bool, summary: str = "", **kv):
        status = "成功" if success else "失败"
        icon = "├─" if success else "├─ ✗"
        self._log("info", "E", f"{icon} Step {step_num} {status}", **kv)
        if summary:
            self._log("debug", "E", f"│ 结果摘要: {_summarize(summary, 300)}")

    def step_retry(self, step_num: int, attempt: int, max_retries: int, error: str):
        self._log("warning", "E", f"│ Step {step_num} 重试 {attempt}/{max_retries}",
                  error=_summarize(error, 200))

    def tool_call(self, tool_name: str, params: dict):
        params_summary = _summarize_params(params)
        self._log("info", "E", f"│ → 工具调用: {tool_name}({params_summary})")
        # 仅在 DEBUG 启用时执行 json.dumps
        if self._logger.isEnabledFor(logging.DEBUG):
            self._log("debug", "T", f"→ {tool_name} 完整参数",
                      params_json=json.dumps(params, ensure_ascii=False, default=str))

    def tool_result(self, tool_name: str, result: str, **kv):
        result_len = len(result)
        items_count = result.count('--- ') if '--- ' in result else None

        extra = {"chars": result_len}
        if items_count:
            extra["items"] = items_count
        extra.update(kv)

        self._log("info", "T", f"← {tool_name} 返回", **extra)
        # 仅在 DEBUG 启用时处理完整结果（避免大字符串格式化）
        if self._logger.isEnabledFor(logging.DEBUG):
            self._log("debug", "T", f"← {tool_name} 完整返回", result_full=result)
            self._log("debug", "T", f"  摘要: {_summarize(result, 300)}")

    def llm_call(self, label: str, duration: float, tokens_in: int, tokens_out: int, **kv):
        total = (tokens_in or 0) + (tokens_out or 0)
        self._log("info", "L", f"LLM 调用 {label}",
                  duration=f"{duration:.1f}s",
                  tokens=f"{tokens_in}+{tokens_out}={total}",
                  **kv)

    def plan(self, steps: list, constraints: list = None):
        self._log("info", "P", f"计划: {len(steps)} 步")
        if constraints:
            self._log("info", "P", f"约束: {constraints}")
        for s in steps:
            num = s.get('step', '?')
            skill = s.get('skill', '?')
            purpose = s.get('purpose', '')
            self._log("info", "P", f"  {num}▶ [{skill}] {purpose}")

    def context_size(self, tokens: int, step: int = 0):
        if tokens > 10000:
            self._log("warning", "E", f"│ Context 膨胀警告", tokens=tokens, step=step)
        else:
            self._log("debug", "E", f"│ Context 大小", tokens=tokens)

    def metrics_summary(self, ctx: 'RequestContext'):
        elapsed = ctx.elapsed()
        m = ctx.metrics
        total_tokens = m['llm_tokens_in'] + m['llm_tokens_out']
        self._log("info", "R", "═" * 50)
        self._log("info", "R", "请求完成汇总",
                  耗时=f"{elapsed:.1f}s",
                  LLM调用=f"{m['llm_calls']}次",
                  Token=f"{total_tokens}",
                  工具调用=f"{m['tool_calls']}次",
                  步骤=f"{m['steps_success']}/{m['steps_total']}成功",
                  重试=f"{m['retries']}次")
        budget = None
        try:
            from utils.budget import BudgetControllerFactory
            budget = BudgetControllerFactory.get_default()
        except Exception:
            pass
        if budget:
            status = budget.get_status()
            self._log("info", "R", "预算使用",
                      Token=f"{status['tokens_percent']}%",
                      调用=f"{status['calls_percent']}%",
                      时间=f"{status['elapsed_seconds']}s/{status['time_limit']}s")
        self._log("info", "R", "═" * 50)


def _summarize(text: str, max_len: int = 200) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text.replace('\n', ' ')
    truncated = text[:max_len]
    for sep in ['。', '.', '\n', '；', ';']:
        last = truncated.rfind(sep)
        if last > max_len * 0.5:
            return truncated[:last + 1].replace('\n', ' ') + "..."
    return truncated.replace('\n', ' ') + "..."


def _summarize_params(params: dict) -> str:
    parts = []
    for k, v in params.items():
        if k in ('query', 'instruction', 'condition', 'user_condition'):
            parts.append(f"{_summarize(str(v), 50)}")
        elif isinstance(v, str) and len(v) > 80:
            parts.append(f"{k}=({len(v)} chars)")
        elif isinstance(v, (list, dict)):
            parts.append(f"{k}=({type(v).__name__} len={len(v)})")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


LOGGER_PREFIXES = ["radar", "stock_data", "sentiment", "aggregator", "test"]


def set_global_log_level(level: str):
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    for prefix in LOGGER_PREFIXES:
        logging.getLogger(prefix).setLevel(numeric_level)
    for logger_name in logging.Logger.manager.loggerDict:
        if any(logger_name.startswith(p + ".") or logger_name == p for p in LOGGER_PREFIXES):
            logging.getLogger(logger_name).setLevel(numeric_level)


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)


def _make_filter() -> _RelativePathFilter:
    return _RelativePathFilter()


def get_logger(user_query: str, skip_db: bool = False) -> tuple:
    """
    为每次对话创建独立 logger + 日志文件
    返回 (RadarLogger, dialog_uuid, RequestContext)

    skip_db=True 时不写入 dialog 数据库、不创建对话日志文件、不写对话头部信息
    （用于系统初始化等非对话场景）
    """
    dialog_uuid = str(uuid.uuid4())

    if not skip_db:
        log_file = os.path.join(Config.get_log_dir(), f"{dialog_uuid}.log")

        with get_db() as conn:
            conn.execute(
                'INSERT INTO dialog (dialog_uuid, user_query, create_time, log_file) VALUES (?,?,?,?)',
                (dialog_uuid, user_query, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), log_file)
            )
    else:
        log_file = None

    raw_logger = logging.getLogger(f"radar.{dialog_uuid[:8]}")
    raw_logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
    raw_logger.handlers.clear()
    raw_logger.addFilter(_make_filter())

    fmt = _make_formatter()

    if log_file:
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setFormatter(fmt)
        fh.addFilter(_make_filter())
        raw_logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(_make_filter())
    raw_logger.addHandler(ch)

    radar_logger = RadarLogger(raw_logger, dialog_uuid)
    ctx = RequestContext(dialog_uuid, raw_logger)
    RequestContext.set_current(ctx)

    if not skip_db:
        radar_logger.info("R", "═" * 50)
        radar_logger.info("R", "新对话开始", uuid=dialog_uuid)
        radar_logger.info("R", f"问题: {user_query}")
        radar_logger.info("R", f"日志: {log_file}")
        radar_logger.info("R", "═" * 50)

    return radar_logger, dialog_uuid, ctx


def get_child_logger(name: str) -> RadarLogger:
    """
    获取子模块 logger（自动携带当前请求的 req_id）。
    所有子模块应使用此方法而非 logging.getLogger。
    """
    ctx = RequestContext.current()
    raw = logging.getLogger(name)
    # 确保子 logger 携带统一的路径 filter（避免重复添加）
    if not any(isinstance(f, _RelativePathFilter) for f in raw.filters):
        raw.addFilter(_make_filter())
    req_id = ctx.req_id if ctx else ""
    return RadarLogger(raw, req_id)


def ensure_radar(logger) -> RadarLogger:
    """确保 logger 是 RadarLogger 实例，否则包装为 RadarLogger"""
    if isinstance(logger, RadarLogger):
        return logger
    return RadarLogger(logger)


def close_logger(radar_logger: RadarLogger):
    """关闭 logger 的所有 handler，释放文件句柄。应在请求结束时调用。"""
    raw = radar_logger._logger
    for handler in raw.handlers[:]:
        handler.close()
        raw.removeHandler(handler)


@contextmanager
def log_phase(radar_logger: RadarLogger, phase_name: str):
    radar_logger.phase(phase_name)
    t0 = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - t0
        radar_logger.debug("R", f"══ 阶段 {phase_name} 结束", elapsed=f"{elapsed:.1f}s")

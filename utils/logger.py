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
import copy
import logging
import logging.handlers
import uuid
import json
import re
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

# ── 共享根 handler 轮转配置（可用环境变量覆盖）──
_LOG_MAX_BYTES = int(os.environ.get("RADAR_LOG_MAX_BYTES", 10 * 1024 * 1024))
_LOG_BACKUP_COUNT = int(os.environ.get("RADAR_LOG_BACKUP_COUNT", 5))
_LOG_RETENTION_DAYS = int(os.environ.get("RADAR_LOG_RETENTION_DAYS", 30))
_LOG_ROTATION_WHEN = os.environ.get("RADAR_LOG_ROTATION_WHEN", "midnight")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)

# ── 并发安全的请求上下文存储（替代类变量 _current）──
_current_ctx: contextvars.ContextVar[Optional['RequestContext']] = contextvars.ContextVar(
    '_current_ctx', default=None
)


# ── 关联检索 Tier 2：从 memory.context 读取当前 dialog_uuid，注入 req_id 列 ──
# 延迟导入 + 缓存，避免在 utils.logger 模块加载期触发 memory 包的重依赖/循环导入。
_DIALOG_UUID_VAR = None


def _get_dialog_uuid_var():
    global _DIALOG_UUID_VAR
    if _DIALOG_UUID_VAR is None:
        try:
            from memory.context import current_dialog_uuid as _v
            _DIALOG_UUID_VAR = _v
        except Exception:
            _DIALOG_UUID_VAR = False
    return _DIALOG_UUID_VAR or None


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
        # 注入 req_id（仅当记录尚未自带），供共享根 handler 的 _SystemFileFormatter 渲染。
        # 未处于任何对话上下文时为 ""（渲染为 req=），不影响既有日志内容。
        if not hasattr(record, "req_id"):
            var = _get_dialog_uuid_var()
            try:
                record.req_id = var.get() if var else ""
            except Exception:
                record.req_id = ""
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
            "llm_tokens_reasoning": 0,
            "llm_cached_tokens": 0,
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

    def record_llm(self, tokens_in: int, tokens_out: int, cached_tokens: int = 0,
                   reasoning_tokens: int = 0):
        self.metrics["llm_calls"] += 1
        self.metrics["llm_tokens_in"] += tokens_in
        self.metrics["llm_tokens_out"] += tokens_out
        self.metrics["llm_tokens_reasoning"] += reasoning_tokens
        self.metrics["llm_cached_tokens"] += cached_tokens

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
        prefix = self._prefix(tag)
        log_method = getattr(self._logger, level)
        log_method(f"{prefix} {message}", stacklevel=stacklevel, exc_info=exc_info,
                   extra={"_prefix": prefix, "_msg": message, "_kv": kv or {}})

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
        extra = dict(kv)
        if summary:
            extra["summary"] = summary
        self._log("info", "E", f"{icon} Step {step_num} {status}", **extra)
        # 统一 metrics 计数
        ctx = RequestContext.current()
        if ctx:
            ctx.record_step(success)

    def step_retry(self, step_num: int, attempt: int, max_retries: int, error: str):
        self._log("warning", "E", f"│ Step {step_num} 重试 {attempt}/{max_retries}",
                  error=error)

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
        extra["result_preview"] = result[:200]
        extra.update(kv)

        self._log("info", "T", f"← {tool_name} 返回", **extra)
        # DEBUG 级别记录完整结果
        if self._logger.isEnabledFor(logging.DEBUG):
            self._log("debug", "T", f"← {tool_name} 完整返回", result_full=result)

    def llm_call(self, label: str, duration: float, tokens_in: int, tokens_out: int,
                 cached_tokens: int = 0, cache_hit_ratio: float = 0.0, **kv):
        total = (tokens_in or 0) + (tokens_out or 0)
        cache_part = f" cached={cached_tokens} hit={cache_hit_ratio:.1%}" if cached_tokens else ""
        self._log("info", "L", f"LLM 调用 {label}",
                  duration=f"{duration:.1f}s",
                  tokens=f"{tokens_in}+{tokens_out}={total}{cache_part}",
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

    def metrics_summary(self, snapshot_or_ctx):
        """输出运行汇总。接受 RunTelemetrySnapshot dict 或 RequestContext（兼容旧调用）。"""
        if isinstance(snapshot_or_ctx, dict):
            self._metrics_summary_from_snapshot(snapshot_or_ctx)
        else:
            self._metrics_summary_from_ctx(snapshot_or_ctx)

    def _metrics_summary_from_snapshot(self, snap: dict):
        elapsed = snap.get("elapsed_seconds", 0)
        m = snap.get("metrics", {})
        total_tokens = m.get('llm_tokens_in', 0) + m.get('llm_tokens_out', 0)
        cached = m.get('llm_cached_tokens', 0)
        reasoning = m.get('llm_tokens_reasoning', 0)
        reasoning_part = f"(思考={reasoning})" if reasoning else ""
        cache_part = f" │ 缓存命中={cached}({cached / m.get('llm_tokens_in', 1) * 100:.0f}%)" if cached else ""
        self._log("info", "R", "═" * 50)
        self._log("info", "R", "请求完成汇总",
                  耗时=f"{elapsed:.1f}s",
                  LLM调用=f"{m.get('llm_calls', 0)}次",
                  Token=f"{m.get('llm_tokens_in', 0)}+{m.get('llm_tokens_out', 0)}={total_tokens}{reasoning_part}{cache_part}",
                  工具调用=f"{m.get('tool_calls', 0)}次",
                  步骤=f"{m.get('steps_success', 0)}/{m.get('steps_total', 0)}成功",
                  重试=f"{m.get('retries', 0)}次")
        # trace 侧缓存统计（更准确，来自 extract_token_usage）
        trace_totals = snap.get("trace_totals", {})
        trace_cached = trace_totals.get("cached_tokens", 0)
        trace_hit = trace_totals.get("cache_hit_ratio", 0)
        if trace_cached:
            self._log("info", "R", "缓存统计",
                      缓存Token=f"{trace_cached}",
                      命中率=f"{trace_hit:.1%}",
                      输入Token=f"{trace_totals.get('input_tokens', 0)}")
        budget_snap = snap.get("budget")
        if budget_snap:
            self._log("info", "R", "预算使用",
                      Token=f"{budget_snap['tokens']}/{budget_snap['tokens_limit']}({budget_snap['tokens_percent']}%, 剩余{budget_snap['tokens_remaining']})",
                      调用=f"{budget_snap['calls']}/{budget_snap['calls_limit']}({budget_snap['calls_percent']}%, 剩余{budget_snap['calls_remaining']})",
                      时间=f"{budget_snap['elapsed_seconds']:.1f}s/{budget_snap['time_limit']}s(剩余{budget_snap['time_remaining']:.1f}s)")
        else:
            self._log("info", "R", "预算使用", 状态="未绑定预算控制器")
        # 统计校验
        consistency = snap.get("consistency", {})
        trace_total = trace_totals.get("total_tokens", 0)
        budget_total = budget_snap["tokens"] if budget_snap else total_tokens
        status_str = consistency.get("metrics_vs_trace_tokens", "unknown")
        delta = consistency.get("token_delta", 0)
        self._log("info", "R", "统计校验",
                  metrics=f"{total_tokens}",
                  trace=f"{trace_total}",
                  budget=f"{budget_total}",
                  状态=f"{'一致' if status_str == 'ok' else '不一致'}",
                  差异=f"{delta}" if status_str != "ok" else "0")
        self._log("info", "R", "═" * 50)

    def _metrics_summary_from_ctx(self, ctx: 'RequestContext'):
        """兼容旧调用方式：直接传 RequestContext"""
        elapsed = ctx.elapsed()
        m = ctx.metrics
        total_tokens = m['llm_tokens_in'] + m['llm_tokens_out']
        cached = m.get('llm_cached_tokens', 0)
        reasoning = m.get('llm_tokens_reasoning', 0)
        reasoning_part = f"(思考={reasoning})" if reasoning else ""
        cache_part = f" │ 缓存命中={cached}({cached / m['llm_tokens_in'] * 100:.0f}%)" if cached and m['llm_tokens_in'] else ""
        self._log("info", "R", "═" * 50)
        self._log("info", "R", "请求完成汇总",
                  耗时=f"{elapsed:.1f}s",
                  LLM调用=f"{m['llm_calls']}次",
                  Token=f"{total_tokens}{reasoning_part}{cache_part}",
                  工具调用=f"{m['tool_calls']}次",
                  步骤=f"{m['steps_success']}/{m['steps_total']}成功",
                  重试=f"{m['retries']}次")
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


LOGGER_PREFIXES = ["radar", "stock_data", "sentiment", "aggregator", "test", "memory"]


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


class _ConsoleFormatter(logging.Formatter):
    """控制台 Formatter — 对 kv values 截断。使用 copy-on-format 避免污染原始 record。"""

    def __init__(self):
        super().__init__(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    def format(self, record):
        if hasattr(record, '_kv') and record._kv:
            kv_str = " │ ".join(
                f"{k}={_summarize(str(v), 300)}" if isinstance(v, str) and len(v) > 300 else f"{k}={v}"
                for k, v in record._kv.items()
            )
            prefix = getattr(record, '_prefix', '')
            msg = getattr(record, '_msg', record.getMessage())
            record_copy = copy.copy(record)
            record_copy.msg = f"{prefix} {msg} │ {kv_str}"
            record_copy.args = None
            return super().format(record_copy)
        return super().format(record)


class _FileFormatter(logging.Formatter):
    """文件 Formatter — 原样输出，不截断。剥离 ANSI 转义码。使用 copy-on-format 避免污染原始 record。"""

    def __init__(self):
        super().__init__(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    def format(self, record):
        if hasattr(record, '_kv') and record._kv:
            kv_str = " │ ".join(
                f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" if isinstance(v, (dict, list)) else f"{k}={v}"
                for k, v in record._kv.items()
            )
            prefix = getattr(record, '_prefix', '')
            msg = getattr(record, '_msg', record.getMessage())
            record_copy = copy.copy(record)
            record_copy.msg = f"{prefix} {msg} │ {kv_str}"
            record_copy.args = None
            return _strip_ansi(super().format(record_copy))
        return _strip_ansi(super().format(record))


# ── 关联检索 Tier 2：共享根 handler 专用 Formatter ──
# 在 _FileFormatter 基础上增加 req_id 列，使系统级日志（logs/radar-system.log）
# 可按 dialog_uuid 检索。对话级日志仍用 _FileFormatter（不加 req_id 列，保持原格式）。
_LOG_FORMAT_SYS = '%(asctime)s.%(msecs)03d │ %(levelname)-7s │ req=%(req_id)s │ %(relpath)s:%(lineno)d │ %(funcName)s │ %(message)s'


class _SystemFileFormatter(logging.Formatter):
    """共享根 handler 专用 Formatter：在 _FileFormatter 基础上增加 req_id 列。"""

    def __init__(self):
        super().__init__(_LOG_FORMAT_SYS, datefmt=_DATE_FORMAT)

    def format(self, record):
        if hasattr(record, '_kv') and record._kv:
            kv_str = " │ ".join(
                f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" if isinstance(v, (dict, list)) else f"{k}={v}"
                for k, v in record._kv.items()
            )
            prefix = getattr(record, '_prefix', '')
            msg = getattr(record, '_msg', record.getMessage())
            record_copy = copy.copy(record)
            record_copy.msg = f"{prefix} {msg} │ {kv_str}"
            record_copy.args = None
            return _strip_ansi(super().format(record_copy))
        return _strip_ansi(super().format(record))


_ROOT_HANDLER_MARKER = "_radar_shared_root_handler"


def clean_old_logs(log_dir: str = None, retention_days: int = 30):
    """清理超过 retention_days 天的旧日志目录和文件。

    每次系统启动时由 install_root_handler 自动调用一次。
    """
    if log_dir is None:
        log_dir = get_logs_dir()
    if not os.path.isdir(log_dir):
        return
    cutoff = time.time() - retention_days * 86400
    deleted_dirs = 0
    deleted_files = 0

    for entry in os.listdir(log_dir):
        entry_path = os.path.join(log_dir, entry)
        try:
            if os.path.isdir(entry_path):
                # 按日期命名的目录 (YYYY-MM-DD)
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", entry):
                    continue
                # 目录下所有文件都超过保留期才删除整个目录
                all_old = True
                for f in os.listdir(entry_path):
                    fp = os.path.join(entry_path, f)
                    if os.path.getmtime(fp) > cutoff:
                        all_old = False
                        break
                if all_old:
                    import shutil
                    shutil.rmtree(entry_path, ignore_errors=True)
                    deleted_dirs += 1
            elif os.path.isfile(entry_path):
                # 轮转产生的备份日志 (radar-system.log.YYYY-MM-DD)
                if entry.startswith("radar-system.log.") and os.path.getmtime(entry_path) < cutoff:
                    os.remove(entry_path)
                    deleted_files += 1
        except OSError:
            pass

    if deleted_dirs or deleted_files:
        # 用 root logger，此时根 handler 可能尚未安装，直接用 logging
        logging.getLogger(__name__).info(
            "[LOG-CLEAN] 清理 %d 个旧日志目录, %d 个备份文件 (保留期=%d天)",
            deleted_dirs, deleted_files, retention_days,
        )


def install_root_handler(max_bytes: int = None, backup_count: int = None) -> Optional[logging.Handler]:
    """幂等安装共享根 handler：所有未独立落盘日志的兜底落盘点。

    覆盖此前默认丢失（仅靠 Python lastResort 把 WARNING+ 打到 stderr）的记录：
      - 子模块 get_child_logger(...) 记录
      - 裸 logging.getLogger(...) 调用（如 server/bootstrap.py 的 _logger）
      - 系统初始化 get_logger(skip_db=True) 的无文件日志

    设计要点：
      - 挂在 root logger，propagate=True 的记录都会经过它
      - 对话级 logger（skip_db=False）在 get_logger 中已设 propagate=False，不会重复落盘
      - RotatingFileHandler + 级别过滤，避免调试噪声撑爆磁盘
      - 幂等：重复调用（bootstrap 多次执行）不会重复挂 handler
    """
    root = logging.getLogger()
    if getattr(root, _ROOT_HANDLER_MARKER, False):
        return None
    if max_bytes is None:
        max_bytes = _LOG_MAX_BYTES
    if backup_count is None:
        backup_count = _LOG_BACKUP_COUNT
    log_dir = get_logs_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "radar-system.log")
    fh = logging.handlers.TimedRotatingFileHandler(
        log_file,
        encoding="utf-8",
        when=_LOG_ROTATION_WHEN,
        backupCount=backup_count,
    )
    fh.setFormatter(_SystemFileFormatter())
    fh.addFilter(_make_filter())
    root.addHandler(fh)
    setattr(root, _ROOT_HANDLER_MARKER, True)

    # 启动时清理过期日志
    clean_old_logs(log_dir, retention_days=_LOG_RETENTION_DAYS)

    return fh


def get_logger(user_query: str, skip_db: bool = False, console: bool = True) -> tuple:
    """
    为每次对话创建独立 logger + 日志文件
    返回 (RadarLogger, dialog_uuid, RequestContext, log_file)

    skip_db=True 时不写入 dialog 数据库、不创建对话日志文件、不写对话头部信息
    （用于系统初始化等非对话场景）
    console=False 时不添加 StreamHandler，日志只写文件（CLI 用于抑制终端输出）
    """
    dialog_uuid = str(uuid.uuid4())
    now = datetime.now()

    if not skip_db:
        # 按日期创建子目录，文件名格式：日期-时间-uuid.log
        date_dir = now.strftime('%Y-%m-%d')
        log_dir = os.path.join(Config.get_log_dir(), date_dir)
        os.makedirs(log_dir, exist_ok=True)

        time_prefix = now.strftime('%Y-%m-%d-%H-%M')
        log_file = os.path.join(log_dir, f"{time_prefix}-{dialog_uuid}.log")

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

    if log_file:
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setFormatter(_FileFormatter())
        fh.addFilter(_make_filter())
        raw_logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(_ConsoleFormatter())
        ch.addFilter(_make_filter())
        raw_logger.addHandler(ch)

    # 对话级日志已独立落盘（dialog 文件 + 控制台），关闭向 root 传播以免与共享根 handler 重复
    raw_logger.propagate = (log_file is None)

    radar_logger = RadarLogger(raw_logger, dialog_uuid)
    ctx = RequestContext(dialog_uuid, raw_logger)
    RequestContext.set_current(ctx)

    if not skip_db:
        radar_logger.info("R", "═" * 50)
        radar_logger.info("R", "新对话开始", uuid=dialog_uuid)
        radar_logger.info("R", f"问题: {user_query}")
        radar_logger.info("R", f"日志: {log_file}")
        radar_logger.info("R", "═" * 50)

    return radar_logger, dialog_uuid, ctx, log_file


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


# 模块导入即幂等安装共享根 handler，确保所有日志体系（radar/task/web/裸 getLogger）都有兜底落盘点
install_root_handler()

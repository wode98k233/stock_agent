"""Web 调用 Agent 的公共执行封装。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from config import Config
# 注意：`agents.factory` 会在模块级导入 `agents.base` → `utils.memory` → torch 全家桶（~20s）。
# 为让 web 秒级起服（lifespan 不阻塞），此处的 AgentFactory 改为函数内懒导入，
# 真正的 torch 加载由后台预热线程完成；首请求经 require_agent_ready await 兜底。
from agents.shared.budget_ctx import budget_decision_ctx
from memory.context import current_dialog_uuid, current_session_id
from tools.skills import SkillRegistry
from utils.app_paths import get_logs_dir
from memory.metadata import reset as _reset_mem_meta
from utils.logger import close_logger, get_logger
from utils.progress import ProgressEvent  # noqa: F401（类型注解用，from __future__ import annotations 后仅在 IDE/类型检查生效）


def _get_agent_factory():
    """懒获取 AgentFactory：首次调用才触发 torch 依赖链导入（避免阻塞 web 启动）。"""
    from agents.factory import AgentFactory
    return AgentFactory


@dataclass
class WebAgentContext:
    skill_register: object
    session_stats: object | None = None
    _memory_pool: dict = field(default_factory=dict)
    _logger_factory: object | None = None
    memory: object = None
    memory_sdk: object | None = None          # MemorySDK 实例

    def get_memory(self, dialog_uuid: str, storage=None):
        if dialog_uuid in self._memory_pool:
            return self._memory_pool[dialog_uuid]

        from utils.memory import MemoryManager
        if self._logger_factory is not None:
            logger, _, _, _ = self._logger_factory("web-memory", skip_db=True)
        else:
            logger, _, _, _ = get_logger("web-memory", skip_db=True)
        memory = MemoryManager(logger)

        if storage is not None:
            self._replay_history(memory, dialog_uuid, storage)

        self._memory_pool[dialog_uuid] = memory
        return memory

    @staticmethod
    def _replay_history(memory, dialog_uuid: str, storage):
        if not memory.enabled:
            return
        msgs = storage.list_messages(dialog_uuid)
        # 排除最后一条 user 消息（当前轮，会由 classify_input 单独添加）
        if msgs and msgs[-1]["role"] == "user":
            msgs = msgs[:-1]
        pending_user = None
        for message in msgs:
            if message["role"] == "user" and message.get("content"):
                pending_user = message["content"]
            elif (
                pending_user
                and message["role"] == "assistant"
                and message.get("content")
                and message.get("status") == "completed"
            ):
                memory.append_turn(pending_user, message["content"])
                pending_user = None

    def clear_memory(self, dialog_uuid: str):
        mem = self._memory_pool.pop(dialog_uuid, None)
        if mem:
            mem.clear()


@dataclass
class AgentRunResult:
    response: str
    log_uuid: str
    log_file: str | None = None
    trace_run_id: str | None = None


def _archive_memory(context, user_input: str, response: str,
                    dialog_uuid: str, logger, budget_ctx) -> None:
    """将 agent 执行结果归档到记忆系统。

    仅归档有效分析结果：跳过过短响应（<200 字符）和已知错误模式。
    防止 Planner 阶段 LLM 故障等场景产生垃圾记忆 chunk。
    """
    # ── 门禁：跳过明显无效的响应 ──
    if not response or len(response) < 200:
        logger.info(f"[archive] SKIP: response too short "
                    f"({len(response) if response else 0} chars), not a valid analysis")
        return
    _ERROR_PREFIXES = (
        "分析过程出错：",
        "分析因",
        "无法生成执行计划",
        "抱歉，无法",
    )
    if response.strip().startswith(_ERROR_PREFIXES):
        logger.info("[archive] SKIP: response is an error/fallback message")
        return

    try:
        sdk = getattr(context, "memory_sdk", None)
        if sdk is None:
            logger.warning("[archive] SKIP: memory_sdk is None on context")
            return
        current_dialog_uuid.set(str(dialog_uuid))
        current_session_id.set(str(dialog_uuid))  # 一个 dialog 就是一个 session
        sdk.archive(user_input, response, None, logger=logger)
        logger.info(f"[archive] OK: backend={sdk.backend.name()} count={sdk.backend.count()} query={user_input[:50]}")
    except Exception as e:
        logger.error(f"[archive] FAIL: {e}")


async def run_web_agent_query(
    *,
    user_input: str,
    mode: str,
    context: WebAgentContext,
    dialog_uuid: str = "",
    storage=None,
    progress_callback: Optional[Callable[[ProgressEvent], None]] = None,
    budget_decision_callback: Optional[Callable[[dict], str]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    title_callback: Optional[Callable[[str], None]] = None,
    title_source: str = "",
) -> AgentRunResult:
    logger, log_uuid, budget_ctx, log_file = get_logger(user_input)
    _captured_trace_run_id: str | None = None

    memory = context.get_memory(dialog_uuid, storage)

    def _wrapped_callback(event: ProgressEvent):
        nonlocal _captured_trace_run_id
        if _captured_trace_run_id is None:
            try:
                from utils.agent_trace.core import _active_recorder
                recorder = _active_recorder.get()
                if recorder and recorder._root:
                    _captured_trace_run_id = recorder._root
            except Exception:
                pass
        if progress_callback:
            progress_callback(event)

    # 设置 web 端预算决策回调，agent 内部通过 ContextVar 检测
    _token = budget_decision_ctx.set(budget_decision_callback)
    # 协作式取消：agent 在检查点（LLM 调用前/ReAct 循环/工具批次）读取
    from utils.cancellation import reset_cancel_check, set_cancel_check
    _cancel_tok = set_cancel_check(cancel_check)
    # 原始提问（enrich 前）：澄清「重新描述」回填输入框用
    from agents.shared.raw_input_ctx import reset_raw_input, set_raw_input
    _raw_tok = set_raw_input(user_input)
    # 关联检索 Tier 2：dialog_uuid 注入日志上下文的 token（在 try 内设置、finally 内重置）
    _dlg_tok = _sid_tok = _title_tok = None
    try:
        # 关联检索 Tier 2：将本次 Web 请求的 dialog_uuid 注入日志上下文，
        # 使 agent 运行期产生的系统级日志（logs/radar-system.log）携带 req_id，
        # 可按 dialog_uuid 检索。该 ContextVar 位于 asyncio.run 的隔离上下文中，
        # 函数退出（finally reset）后不泄漏到线程池复用。
        _dlg_tok = current_dialog_uuid.set(str(dialog_uuid))
        _sid_tok = current_session_id.set(str(dialog_uuid))

        # 会话标题：向 agent 层注入 TitleRequest（classify 生成标题后回调落库/推送）
        if title_callback is not None and str(title_source or "").strip():
            from agents.shared.title_ctx import TitleRequest, set_title_request
            _title_tok = set_title_request(
                TitleRequest(first_question=str(title_source).strip(), callback=title_callback)
            )

        # 开发模式：每次请求前热重载 Agent 模块 + Config
        if Config.WEB_DEV_RELOAD:
            Config.reload()
            from agents import reload_agents
            reload_agents()

        # ── 重置记忆元数据 ContextVar (避免跨请求污染) ──
        try:
            _reset_mem_meta()
        except Exception:
            pass

        agent = _get_agent_factory().get(mode)
        agent.on_startup()
        registry = SkillRegistry(logger, memory, context.skill_register)
        response = await agent.run(
            user_input,
            registry,
            memory,
            logger,
            progress_callback=_wrapped_callback,
            dialog_uuid=dialog_uuid,
        )

        # 记忆归档：agent 执行完毕后写入语义+情景记忆
        _archive_memory(context, user_input, response, dialog_uuid, logger, budget_ctx)

        session_stats = context.session_stats
        if session_stats and budget_ctx:
            session_stats.accumulate(budget_ctx)

        from utils.agent_trace.db import find_latest_trace_run_id  # 函数内懒导入（避免拖慢启动）
        trace_run_id = _captured_trace_run_id or find_latest_trace_run_id()

        # 转换为相对路径（相对于 logs 目录）
        relative_log_file = None
        if log_file:
            from utils.app_paths import get_logs_dir
            logs_dir = get_logs_dir()
            if log_file.startswith(logs_dir):
                relative_log_file = "logs/" + log_file[len(logs_dir):].lstrip("/\\").replace("\\", "/")
            else:
                relative_log_file = log_file.replace("\\", "/")

        return AgentRunResult(
            response=response,
            log_uuid=log_uuid,
            log_file=relative_log_file,
            trace_run_id=trace_run_id,
        )
    finally:
        budget_decision_ctx.reset(_token)
        reset_cancel_check(_cancel_tok)
        reset_raw_input(_raw_tok)
        if _dlg_tok is not None:
            current_dialog_uuid.reset(_dlg_tok)
        if _sid_tok is not None:
            current_session_id.reset(_sid_tok)
        if _title_tok is not None:
            from agents.shared.title_ctx import reset_title_request
            reset_title_request(_title_tok)
        close_logger(logger)

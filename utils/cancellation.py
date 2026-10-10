"""协作式取消：用户「停止」正在运行的 agent 任务。

机制与预算决策同款（worker 线程不可强杀）：API 线程置标志，agent 在检查点
（每次 LLM 调用前、ReAct 循环/工具批次边界）调用 ``raise_if_cancelled()``
抛 ``AgentCancelled``，由 ``TaskRuntime._run_task`` 捕获后落库并推 SSE。

注意：单次 LLM/工具调用本身跑完才会到下一个检查点（Python 线程无法中断），
停止是「下一次检查点生效」，省掉的是后续轮次的 token。
"""
import contextvars
from typing import Callable, Optional


class AgentCancelled(BaseException):
    """用户主动停止任务（协作式取消，检查点生效）。

    刻意继承 BaseException：让它穿过各层 ``except Exception`` 兜底
    （ReAct 异常处理器、JSON 重试、工具错误包装等），直达 ``_run_task``；
    语义与 CancelledError 同类，业务代码不应吞掉。
    """


# Web 端注入：返回 True 表示当前任务已被取消；None = 无取消通道（CLI/TUI/测试）
cancel_check_ctx: contextvars.ContextVar[Optional[Callable[[], bool]]] = contextvars.ContextVar(
    "cancel_check_ctx", default=None
)


def set_cancel_check(check: Optional[Callable[[], bool]]):
    """设置当前上下文的取消检查回调，返回 token 供 reset。"""
    return cancel_check_ctx.set(check)


def reset_cancel_check(token) -> None:
    """恢复取消检查回调（配合 set_cancel_check 的 token）。"""
    cancel_check_ctx.reset(token)


def raise_if_cancelled() -> None:
    """检查点调用：已取消则抛 AgentCancelled；检查回调异常时静默放行。"""
    check = cancel_check_ctx.get()
    if check is None:
        return
    try:
        cancelled = bool(check())
    except Exception:
        return
    if cancelled:
        raise AgentCancelled("用户已停止任务")

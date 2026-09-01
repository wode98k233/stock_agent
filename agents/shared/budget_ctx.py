"""Web 端预算决策回调的 ContextVar

用于在 agent 执行过程中检测当前是否处于 Web 模式，
如果是，则使用回调代替 input() 进行用户交互。

放在 agents/shared/ 避免 server/ 和 agents/ 的循环依赖。
"""
import contextvars
from typing import Callable, Optional

budget_decision_ctx: contextvars.ContextVar[Optional[Callable]] = contextvars.ContextVar(
    'budget_decision_ctx', default=None
)

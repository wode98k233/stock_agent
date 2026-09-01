"""Agent 群模式异常处理链"""
from agents.group.state import GroupState


class GroupException:
    def __init__(self, message: str, state: GroupState = None):
        self.message = message
        self.state = state


class BudgetExceedGroupException(GroupException):
    pass


class EmptyPlanGroupException(GroupException):
    pass


class AllAgentsFailedGroupException(GroupException):
    pass


class GroupExceptionHandler:
    def can_handle(self, exc: GroupException) -> bool:
        raise NotImplementedError

    def handle(self, exc: GroupException) -> str:
        raise NotImplementedError


class BudgetExceedHandler(GroupExceptionHandler):
    def can_handle(self, exc: GroupException) -> bool:
        return isinstance(exc, BudgetExceedGroupException)

    def handle(self, exc: GroupException) -> str:
        state = exc.state
        if state and state.get("accumulated_data"):
            return f"预算超限，已基于已有数据生成总结:\n\n{state['accumulated_data']}"
        return "预算超限，无可用结果"


class EmptyPlanHandler(GroupExceptionHandler):
    def can_handle(self, exc: GroupException) -> bool:
        return isinstance(exc, EmptyPlanGroupException)

    def handle(self, exc: GroupException) -> str:
        return f"无法生成有效计划: {exc.message}"


class AllAgentsFailedHandler(GroupExceptionHandler):
    def can_handle(self, exc: GroupException) -> bool:
        return isinstance(exc, AllAgentsFailedGroupException)

    def handle(self, exc: GroupException) -> str:
        state = exc.state
        if state and state.get("accumulated_data"):
            return f"所有 Agent 执行失败，已基于已有数据生成总结:\n\n{state['accumulated_data']}"
        return "所有 Agent 执行失败，无可用结果"


class GenericGroupHandler(GroupExceptionHandler):
    def can_handle(self, exc: GroupException) -> bool:
        return True

    def handle(self, exc: GroupException) -> str:
        return f"执行异常: {exc.message}"


class GroupExceptionHandlerChain:
    def __init__(self):
        self._handlers = [
            BudgetExceedHandler(),
            EmptyPlanHandler(),
            AllAgentsFailedHandler(),
            GenericGroupHandler(),
        ]

    async def handle(self, exc, handler_ctx=None) -> str:
        if not isinstance(exc, GroupException):
            exc = GroupException(str(exc))
        for handler in self._handlers:
            if handler.can_handle(exc):
                return handler.handle(exc)
        return f"未知异常: {exc}"

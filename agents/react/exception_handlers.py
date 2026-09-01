"""
选股雷达 - ReAct Agent 异常处理策略
使用策略模式将异常处理逻辑拆分为独立的 Handler 类
"""
from abc import ABC, abstractmethod
from typing import Optional

from utils.budget import BudgetExceeded, BudgetExemptWindow
from utils.logger import ensure_radar
from agents.common import generate_partial_summary
from config import Config


class ExceptionHandler(ABC):
    """
    异常处理策略基类
    每个子类处理特定类型的异常
    """

    @abstractmethod
    def can_handle(self, exception: Exception) -> bool:
        """判断当前 handler 是否能处理该异常"""
        ...

    @abstractmethod
    async def handle(self, exception: Exception, context: "HandlerContext") -> str:
        """处理异常并返回结果字符串"""
        ...


class HandlerContext:
    """Handler 执行上下文"""

    def __init__(
        self,
        user_input: str,
        exec_state,
        llm,
        logger,
        budget,
        graph,
        run_ctx,
        handle_budget_exceeded_callback,
        thread_id=None,
    ):
        self.user_input = user_input
        self.exec_state = exec_state
        self.llm = llm
        self.logger = ensure_radar(logger)
        self.budget = budget
        self.graph = graph
        self.run_ctx = run_ctx
        self.handle_budget_exceeded_callback = handle_budget_exceeded_callback
        self.thread_id = thread_id


class BudgetExceededHandler(ExceptionHandler):
    """处理预算超限异常"""

    def can_handle(self, exception: Exception) -> bool:
        return isinstance(exception, BudgetExceeded)

    async def handle(self, exception: Exception, context: HandlerContext) -> str:
        error = exception
        context.logger.warning("B", f"预算超限: {error}")

        if context.exec_state.has_useful_results():
            user_choice = await context.handle_budget_exceeded_callback(
                context.user_input, context.exec_state, context.llm, context.logger, error
            )
            if user_choice == "":
                exempt = BudgetExemptWindow(calls_limit=Config.BUDGET_EXEMPT_CALLS_LIMIT, time_limit=Config.BUDGET_EXEMPT_TIME_LIMIT)
                context.budget.set_exempt_window(exempt)
                context.logger.info("B", "用户确认继续，创建预算豁免窗口，从 checkpoint 恢复")
                try:
                    # 标记 checkpoint 恢复，让 AgentNode 从 messages 重建 exec_state
                    context.exec_state._checkpoint_restored = True
                    # 从 checkpoint 恢复继续执行，budget 豁免通过闭包已生效
                    invoke_config = {
                        "configurable": {"thread_id": context.thread_id},
                        "callbacks": context.run_ctx.build_callbacks("react"),
                    }
                    resp = await context.graph.ainvoke(
                        None,  # 从 checkpoint 恢复，不传 initial_state
                        config=invoke_config,
                    )
                    return resp.get("final_result", "")
                except BudgetExceeded as e2:
                    context.logger.warning("B", f"豁免后仍超限: {e2}")
                    return await generate_partial_summary(
                        context.user_input, context.exec_state, context.llm, context.logger, f"预算超限({e2})"
                    )
                except Exception as e3:
                    # checkpoint 恢复失败（如序列化错误、节点异常等），用已有数据生成总结
                    context.logger.warning("B", f"checkpoint 恢复失败: {e3}")
                    return await generate_partial_summary(
                        context.user_input, context.exec_state, context.llm, context.logger, f"恢复失败({e3})"
                    )
            else:
                return user_choice

        return await generate_partial_summary(
            context.user_input, context.exec_state, context.llm, context.logger, f"预算超限({error})"
        )


class GraphRecursionHandler(ExceptionHandler):
    """处理图迭代上限异常"""

    def can_handle(self, exception: Exception) -> bool:
        from langgraph.errors import GraphRecursionError
        return isinstance(exception, GraphRecursionError)

    async def handle(self, exception: Exception, context: HandlerContext) -> str:
        context.logger.warning("R", f"达到迭代上限: {exception}")

        if context.exec_state.has_useful_results():
            return await generate_partial_summary(
                context.user_input, context.exec_state, context.llm, context.logger, "达到迭代限制"
            )
        return "分析因达到迭代限制提前结束，请尝试更具体的问题。"


class GenericExceptionHandler(ExceptionHandler):
    """处理通用异常，作为兜底策略"""

    def can_handle(self, exception: Exception) -> bool:
        return True

    async def handle(self, exception: Exception, context: HandlerContext) -> str:
        import traceback
        context.logger.error("R", f"ReAct Agent 执行失败", error=str(exception) + traceback.format_exc())

        if context.exec_state.has_useful_results():
            result = await generate_partial_summary(
                context.user_input, context.exec_state, context.llm, context.logger, "执行异常"
            )
        else:
            result = f"分析过程遇到问题：{exception} {traceback.format_exc()}"

        context.run_ctx.end_trace(result or "", status="error", error=str(exception))
        return result or "分析过程中未生成有效结果，请尝试更具体的问题。"


class ExceptionHandlerChain:
    """
    异常处理链
    按顺序遍历 handlers，找到第一个能处理异常的 handler 并执行
    """

    def __init__(self, handlers: Optional[list[ExceptionHandler]] = None):
        self._handlers: list[ExceptionHandler] = handlers or []

    def add_handler(self, handler: ExceptionHandler) -> "ExceptionHandlerChain":
        self._handlers.append(handler)
        return self

    async def handle(self, exception: Exception, context: HandlerContext) -> str:
        for handler in self._handlers:
            if handler.can_handle(exception):
                return await handler.handle(exception, context)
        raise RuntimeError(f"没有 handler 能处理异常: {exception}")


def build_default_handler_chain() -> ExceptionHandlerChain:
    """构建默认的异常处理链（按优先级排序）"""
    return ExceptionHandlerChain([
        BudgetExceededHandler(),
        GraphRecursionHandler(),
        GenericExceptionHandler(),
    ])

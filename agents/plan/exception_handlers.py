"""Plan/PDOR Agent 异常处理策略

复用 ReAct 的 ExceptionHandler/ExceptionHandlerChain 基础设施，
实现 Plan/PDOR 特化的异常处理逻辑。
"""
from agents.react.exception_handlers import (
    ExceptionHandler,
    ExceptionHandlerChain,
    HandlerContext,
)
from utils.budget import BudgetExceeded, BudgetExemptWindow
from utils.logger import ensure_radar
from config import Config


class PlanHandlerContext(HandlerContext):
    """Plan/PDOR 专用上下文 — 扩展 base HandlerContext

    额外字段：
        app: LangGraph compiled graph（用于 checkpoint 恢复）
        config: invoke config（含 thread_id、callbacks）
        last_state: 最后一次执行的状态快照
        extract_completed_steps: 从 last_state 提取已完成步骤的回调
    """

    def __init__(
        self,
        *,
        app=None,
        config=None,
        last_state=None,
        extract_completed_steps=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.app = app
        self.config = config
        self.last_state = last_state or {}
        self.extract_completed_steps = extract_completed_steps or (lambda: [])


class PlanBudgetExceededHandler(ExceptionHandler):
    """Plan/PDOR 预算超限处理 — checkpoint 恢复"""

    def can_handle(self, exception: Exception) -> bool:
        return isinstance(exception, BudgetExceeded)

    async def handle(self, exception: Exception, context: PlanHandlerContext) -> str:
        logger = context.logger
        logger.warning("B", f"预算超限: {exception}")

        # 1. 询问用户（回调签名：(last_state, logger, error)）
        decision = context.handle_budget_exceeded_callback(
            context.last_state, logger, exception
        )

        # 2. 用户确认继续 → 豁免窗口 + checkpoint 恢复
        if decision == "":
            exempt = BudgetExemptWindow(
                calls_limit=Config.BUDGET_EXEMPT_CALLS_LIMIT,
                time_limit=Config.BUDGET_EXEMPT_TIME_LIMIT,
            )
            context.budget.set_exempt_window(exempt)
            logger.info("B", "用户确认继续，创建预算豁免窗口，从 checkpoint 恢复")

            from agents.shared.stream_utils import stream_and_collect

            try:
                full_response = await stream_and_collect(
                    context.app, None, context.config, context.last_state
                )
                context.run_ctx.end_trace(full_response, status="success")
                return full_response
            except Exception as recovery_err:
                logger.warning("B", f"checkpoint 恢复失败: {recovery_err}")
                # 降级：基于已有结果生成总结
                result = _generate_summary_from_steps(context, f"恢复失败({recovery_err})")
                context.run_ctx.end_trace(result, status="error")
                return result

        # 3. 用户拒绝 → 基于已有结果生成总结
        if decision:
            context.run_ctx.end_trace(decision, status="success")
            return decision

        # 4. 兜底
        result = _generate_summary_from_steps(context, f"预算超限({exception})")
        context.run_ctx.end_trace(result, status="success")
        return result


class PlanGraphRecursionHandler(ExceptionHandler):
    """Plan/PDOR 迭代上限处理"""

    def can_handle(self, exception: Exception) -> bool:
        from langgraph.errors import GraphRecursionError
        return isinstance(exception, GraphRecursionError)

    async def handle(self, exception: Exception, context: PlanHandlerContext) -> str:
        context.logger.warning("R", f"达到迭代上限: {exception}")
        result = _generate_summary_from_steps(context, "达到迭代上限")
        context.run_ctx.end_trace(result, status="success")
        return result


class PlanGenericExceptionHandler(ExceptionHandler):
    """Plan/PDOR 通用异常处理"""

    _LLM_UNAVAILABLE_HINTS = {
        "quota": "LLM 服务配额不足（insufficient quota），请检查模型配额/充值，或切换其他渠道后重试。",
        "rate_limit": "LLM 服务限流（429），请稍后重试，或降低并发/换高吞吐渠道。",
        "network": "LLM 服务网络/超时异常，请检查服务连通性后重试。",
        "auth": "LLM API Key 无效或未授权，请检查 .env 中的 API 配置。",
    }

    def can_handle(self, exception: Exception) -> bool:
        return True

    async def handle(self, exception: Exception, context: PlanHandlerContext) -> str:
        import traceback
        context.logger.error("R", "执行异常", error=str(exception) + traceback.format_exc())

        # LLM 服务不可用（配额/限流/网络/认证，已重试耗尽）→ 直接终止并给出明确提示，
        # 不再调 LLM 生成总结（必然再次失败，纯空转）。
        from utils.llm_factory import classify_llm_error
        kind = classify_llm_error(exception)
        if kind in self._LLM_UNAVAILABLE_HINTS:
            hint = self._LLM_UNAVAILABLE_HINTS[kind]
            result = f"⚠️ 分析无法继续：{hint}\n\n原始错误：{exception}"
            context.run_ctx.end_trace(result, status="error")
            return result

        completed = context.extract_completed_steps()
        if completed:
            from agents.shared.stream_utils import generate_summary
            try:
                result = generate_summary(
                    context.user_input, "", completed, {}, context.logger
                )
                context.logger.info("R", "成功从异常中恢复并生成总结")
                context.run_ctx.end_trace(result, status="error")
                return result
            except Exception:
                summary = "\n".join([f"- {desc}: {res}" for desc, res in completed])
                result = f"分析过程遇到问题，但已有部分结果:\n\n{summary}"
                context.run_ctx.end_trace(result, status="error")
                return result

        result = f"分析过程出错：{exception}"
        context.run_ctx.end_trace(result, status="error")
        return result


def _generate_summary_from_steps(context: PlanHandlerContext, reason: str) -> str:
    """从已完成步骤生成总结（通用逻辑）"""
    completed = context.extract_completed_steps()
    if not completed:
        return f"分析因{reason}提前结束，请尝试更具体的问题。"

    from agents.shared.stream_utils import generate_summary
    return generate_summary(context.user_input, "", completed, {}, context.logger)


def build_plan_handler_chain() -> ExceptionHandlerChain:
    """构建 Plan/PDOR 默认异常处理链"""
    return ExceptionHandlerChain([
        PlanBudgetExceededHandler(),
        PlanGraphRecursionHandler(),
        PlanGenericExceptionHandler(),
    ])

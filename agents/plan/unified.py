"""UnifiedPlan Agent — 大上下文单步执行 Agent"""
import traceback

from agents.base import BaseAgent
from agents.run_context import AgentRunContext
from agents.plan.graph import create_unified_agent
from agents.plan.shared import stream_and_collect
from utils.logger import ensure_radar


class UnifiedPlanAgent(BaseAgent):
    """大上下文单步执行 Agent"""

    name = "unified_plan"
    description = "大上下文单步执行 Agent，适合中等复杂度的选股分析"

    async def run(self, user_input: str, registry, memory, logger, progress_callback=None):
        logger = ensure_radar(logger)
        user_input = self._enrich_user_input(user_input)

        async with AgentRunContext(self.name, logger, user_input) as run_ctx:
            app = create_unified_agent(
                logger=logger,
                memory_mgr=memory,
                skill_registry=registry,
                progress_callback=progress_callback,
                trace_recorder=run_ctx._recorder,
            )

            config = {"configurable": {"thread_id": run_ctx.run_id}, "recursion_limit": 50}

            inputs = {
                "input": user_input,
                "plan": [],
                "past_steps": [],
                "current_step": 0,
                "progress_reporter": None,
                "user_constraints": "",
                "key_data": {},
            }

            last_state = inputs.copy()

            try:
                full_response = await stream_and_collect(app, inputs, config, last_state)
                run_ctx.end_trace(full_response, status="success")
            except Exception as e:
                logger.error("R", "执行异常", error=str(e) + traceback.format_exc())
                try:
                    from utils.logger import RequestContext
                    _ctx = RequestContext.current()
                    if _ctx and run_ctx._recorder:
                        _ctx.trace_recorder = run_ctx._recorder
                except Exception:
                    pass
                full_response = self._handle_exception(e, user_input, last_state, logger)
                run_ctx.end_trace(full_response, status="error")

        self._update_intent_memory(user_input, full_response, logger)
        return full_response

    def _handle_exception(self, e: Exception, user_input: str, last_state: dict, logger):
        """异常处理 — 有部分结果则总结，否则报错"""
        past = last_state.get("past_steps", [])
        if past:
            try:
                from agents.plan.handler import handle_execution_error
                return handle_execution_error(e, user_input, last_state, logger)
            except Exception:
                summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
                return f"分析过程遇到问题，但已有部分结果:\n\n{summary}"
        return f"分析过程出错：{e}"

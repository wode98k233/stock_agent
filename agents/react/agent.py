"""
选股雷达 - ReAct 动态规划 Agent
自驱式执行，不需要预设计划和 Replanner
"""
import traceback
from typing import Optional, Callable

from agents.base import BaseAgent
from config import Config
from utils.llm_factory import get_llm, TokenCompatibleChatOpenAI
from utils.budget import BudgetExceeded, BudgetLimits, BudgetController
from utils.logger import ensure_radar

from agents.executor_callbacks import ExecutionState
from agents.run_context import AgentRunContext
from agents.agent_context import AgentContext
from agents.common import generate_partial_summary
from agents.react.graph import create_react_graph
from agents.checkpoint_factory import create_checkpointer
from agents.react.exception_handlers import (
    HandlerContext,
    build_default_handler_chain,
)


class ReactStockAgent(BaseAgent):
    """
    ReAct 动态规划 Agent
    自驱式执行，不需要 Planner 和 Replanner
    """

    name = "react_stock"
    display_name = "ReAct"
    description = "动态思考和工具调用，适合简单到中等复杂度查询。"

    def warmup_graph(self, logger=None):
        """预热 Graph 编译：预编译一次以缓存 langgraph 内部结构，完成后 GC 回收。

        首次请求时 compile() 从 ~3s 降到 ~0.3s。
        """
        import gc
        try:
            from agents.checkpoint_factory import create_checkpointer
            llm = get_llm()
            checkpointer = create_checkpointer()
            graph = create_react_graph(
                llm=llm,
                max_iterations=Config.REACT_TOOL_CALLS,
                logger=logger,
                skill_registry=None,
                memory=None,
                budget=BudgetController(BudgetLimits(
                    max_tokens_per_query=Config.MAX_TOKENS_PER_QUERY,
                    max_llm_calls_per_query=Config.MAX_LLM_CALLS_PER_QUERY,
                    max_time_seconds=Config.MAX_TIME_SECONDS,
                )),
                exec_state=ExecutionState(),
                checkpointer=checkpointer,
            )
            del graph, llm, checkpointer  # 不保留预热用的临时对象
            gc.collect()
            if logger:
                logger.debug("[Agent] ReAct graph 预热编译完成")
        except Exception:
            pass  # 预热失败不影响主流程

    async def run(self, user_input: str, registry, memory, logger, progress_callback: Optional[Callable] = None, **kwargs):
        logger = ensure_radar(logger)

        original_user_input = user_input  # 保留原始输入，避免丰富后的文本污染记忆
        user_input = self._enrich_user_input(
            user_input, progress_callback=progress_callback,
            history=memory.get_history() if memory else None,
        )

        ctx = AgentContext(
            logger=logger,
            memory=memory,
            skill_registry=registry,
            budget=BudgetController(BudgetLimits(
                max_tokens_per_query=Config.MAX_TOKENS_PER_QUERY,
                max_llm_calls_per_query=Config.MAX_LLM_CALLS_PER_QUERY,
                max_time_seconds=Config.MAX_TIME_SECONDS,
            )),
            progress_reporter=progress_callback,
        )

        exec_state = ExecutionState()
        result = ""
        checkpointer = create_checkpointer()
        thread_id = None

        with AgentRunContext("react_stock", ctx.logger, user_input, ctx.budget, exec_state) as run_ctx:
            llm: TokenCompatibleChatOpenAI = get_llm()
            thread_id = run_ctx.run_id

            graph = create_react_graph(
                llm=llm,
                max_iterations=Config.REACT_TOOL_CALLS,
                logger=ctx.logger,
                skill_registry=registry,
                memory=memory,
                budget=ctx.budget,
                exec_state=exec_state,
                checkpointer=checkpointer,
            )

            try:
                invoke_config = {
                    "configurable": {"thread_id": thread_id},
                    "callbacks": run_ctx.build_callbacks("react"),
                }
                resp = await graph.ainvoke(
                    user_input=user_input,
                    config=invoke_config,
                )

                if not resp.get("is_stock_related", True) and resp.get("classify_response"):
                    run_ctx.end_trace(resp["classify_response"], status="success")
                    if resp["classify_response"]:
                        ctx.memory.add_ai(resp["classify_response"])
                    return resp["classify_response"]

                result = resp.get("final_result", "")
                limit_reason = resp.get("limit_reason", "")

            except Exception as e:
                handler_ctx = HandlerContext(
                    user_input=user_input,
                    exec_state=exec_state,
                    llm=llm,
                    logger=ctx.logger,
                    budget=ctx.budget,
                    graph=graph,
                    run_ctx=run_ctx,
                    handle_budget_exceeded_callback=self._handle_budget_exceeded,
                    thread_id=thread_id,
                )
                handler_chain = build_default_handler_chain()
                try:
                    result = await handler_chain.handle(e, handler_ctx)
                except Exception as handler_err:
                    # handler chain 本身异常（如 checkpoint 恢复失败），用已有数据兜底
                    ctx.logger.warning("R", f"异常处理链失败: {handler_err}")
                    if exec_state.has_useful_results():
                        result = await generate_partial_summary(
                            user_input, exec_state, llm, ctx.logger, f"异常处理失败({handler_err})"
                        )
                    else:
                        result = f"分析过程遇到问题：{e}"

                # GenericExceptionHandler 处理完后需要直接返回（保持原有逻辑）
                from langgraph.errors import GraphRecursionError
                if not isinstance(e, (BudgetExceeded, GraphRecursionError)):
                    if result:
                        ctx.memory.add_ai(result)
                    return result or "分析过程中未生成有效结果，请尝试更具体的问题。"

            if not result and exec_state.has_useful_results():
                result = await generate_partial_summary(
                    user_input, exec_state, llm, ctx.logger, limit_reason or "未知原因"
                )

            if not result:
                result = "分析过程中未生成有效结果，请尝试更具体的问题。"

            run_ctx.end_trace(result, status="success")

        # 图执行完成后写入 memory，使用原始输入（非丰富后文本），避免历史污染
        ctx.memory.add_user(original_user_input)
        if result:
            ctx.memory.add_ai(result)

        ctx.logger.info("R", f"═══ 最终结果 ═══\n{result}")
        if exec_state.tool_calls:
            ctx.logger.info("R", "工具调用汇总：")
            for i, call in enumerate(exec_state.tool_calls, 1):
                status = "✅" if call.success else "❌"
                output_preview = (call.tool_output or "")[:200]
                ctx.logger.info("R", f"  {status} 工具{i}: {call.tool_name} | 输出: {output_preview}")

        return result

    async def _handle_budget_exceeded(self, user_input: str, exec_state: ExecutionState,
                                       llm, logger, error: BudgetExceeded) -> str:
        """处理预算超限：询问用户是否继续执行（创建豁免窗口）

        统一使用 ask_user_decision（支持 Web/CLI 双模式）。
        """
        from agents.user_decision import ask_user_decision

        tool_count = len(exec_state.tool_calls)
        decision = ask_user_decision(
            header=f"预算超限：{error}",
            status_lines=[f"已获取 {tool_count} 个工具的结果"],
            options=["继续执行（创建豁免窗口）", "基于已有信息生成总结"],
        )

        if "继续" in decision:
            logger.info("B", "用户选择继续执行")
            return ""

        logger.info("B", "用户选择生成总结")
        return await generate_partial_summary(user_input, exec_state, llm, logger, "预算超限")

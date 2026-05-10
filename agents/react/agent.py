"""
选股雷达 - ReAct 动态规划 Agent
自驱式执行，不需要预设计划和 Replanner
"""
import asyncio
import traceback
from typing import Optional, Callable

from agents.base import BaseAgent
from config import Config
from utils.llm_factory import get_llm, llm_json_with_retry, TokenCompatibleChatOpenAI
from utils.budget import BudgetExceeded, BudgetLimits, BudgetController
from utils.logger import RequestContext, ensure_radar
from tools.skills import SkillPromptBuilder
from langchain.agents import create_agent as create_react_agent
from agents.prompts import REACT_SYSTEM_PROMPT, SKILL_SELECTOR_PROMPT, REACT_PARTIAL_SUMMARY_PROMPT
from agents.executor_callbacks import ExecutionState
from agents.run_context import AgentRunContext
from agents.agent_context import AgentContext
from agents.common import classify_input, generate_partial_summary


class ReactStockAgent(BaseAgent):
    """
    ReAct 动态规划 Agent
    自驱式执行，不需要 Planner 和 Replanner
    """

    name = "react_stock"
    description = "ReAct 动态规划 Agent，自驱式执行"

    async def run(self, user_input: str, registry, memory, logger, progress_callback: Optional[Callable] = None):
        logger = ensure_radar(logger)

        user_input = self._enrich_user_input(user_input)

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

        llm: TokenCompatibleChatOpenAI = get_llm()

        classify_result = await classify_input(user_input, ctx.memory, llm, ctx.logger)
        if classify_result.get("response"):
            return classify_result["response"]

        catalog_prompt = SkillPromptBuilder.build_catalog_prompt(ctx.skill_registry)
        selected_skills = await self._select_skills(user_input, catalog_prompt, llm, ctx.logger)

        all_tools = []
        if selected_skills:
            for skill_name in selected_skills:
                tools = ctx.skill_registry.get_tools(skill_name)
                if tools:
                    all_tools.extend(tools)

        if not all_tools:
            ctx.logger.warning("R", "未选中任何技能，加载全部工具")
            all_tools = ctx.skill_registry.get_all_tools()

        tool_descriptions = "\n".join(
            f"- **{t.name}**: {t.description}" for t in all_tools
        )
        system_prompt = REACT_SYSTEM_PROMPT + "\n\n## 可用工具\n\n" + tool_descriptions

        history = ctx.memory.get_history()
        messages = [
            ("system", system_prompt),
            *history,
            ("user", user_input),
        ]

        exec_state = ExecutionState()
        result = ""

        with AgentRunContext("react_stock", ctx.logger, user_input, ctx.budget, exec_state) as run_ctx:
            try:
                agent = create_react_agent(llm, all_tools)

                resp = await agent.ainvoke(
                    {"messages": messages},
                    config={
                        "recursion_limit": Config.REACT_TOOL_CALLS,
                        "callbacks": run_ctx.build_callbacks("react"),
                    }
                )

                final_messages = resp.get("messages", [])
                for msg in reversed(final_messages):
                    if hasattr(msg, 'content') and msg.content:
                        if msg.type == 'ai' and not getattr(msg, 'tool_calls', None):
                            result = msg.content
                            break

                from agents.utils import _is_abnormal_result
                is_abnormal: bool = _is_abnormal_result(result)
                has_useful_results = exec_state.has_useful_results()

                if is_abnormal and has_useful_results:
                    result = await self._ask_and_generate_summary(
                        user_input, exec_state, llm, ctx.logger, "达到迭代限制"
                    )
                elif not result:
                    for msg in reversed(final_messages):
                        if hasattr(msg, 'content') and msg.content and len(msg.content) > 50:
                            result = msg.content
                            break

            except BudgetExceeded as e:
                ctx.logger.warning("B", f"预算超限: {e}")
                has_useful_results = exec_state.has_useful_results()
                if has_useful_results:
                    result = await self._ask_and_generate_summary(
                        user_input, exec_state, llm, ctx.logger, f"预算超限({e})"
                    )
                else:
                    result = f"分析因资源限制提前结束（{e}），请尝试更具体的问题。"
                run_ctx.end_trace(result or "", status="error", error=str(e))
            except Exception as e:
                ctx.logger.error("R", f"ReAct Agent 执行失败", error=str(e) + traceback.format_exc())
                has_useful_results = exec_state.has_useful_results()
                if has_useful_results:
                    result = await self._ask_and_generate_summary(
                        user_input, exec_state, llm, ctx.logger, "执行异常"
                    )
                else:
                    result = f"分析过程遇到问题：{e} {traceback.format_exc()}"
                run_ctx.end_trace(result or "", status="error", error=str(e))
            else:
                run_ctx.end_trace(result or "", status="success")

        if result:
            ctx.memory.add_ai(result)

        return result or "分析过程中未生成有效结果，请尝试更具体的问题。"

    async def _ask_and_generate_summary(self, user_input: str, exec_state: ExecutionState,
                                       llm, logger, reason: str) -> str:
        """询问用户是否基于已有信息总结，并生成总结"""
        collected_info = exec_state.get_summary_context()

        prompt_text = REACT_PARTIAL_SUMMARY_PROMPT.format(
            user_input=user_input,
            collected_info=collected_info
        )
        estimated_chars = len(prompt_text)
        estimated_tokens = estimated_chars // 1.5

        print(f"\n{'='*60}")
        print(f"⚠️ {reason}，但已获取到部分信息。")
        print(f"{'='*60}")
        print(f"已获取 {len(exec_state.tool_calls)} 个工具的结果")
        print(f"预估总结字数：约 {estimated_chars} 字符")
        print(f"预估 Token 消耗：约 {int(estimated_tokens)} 个")
        print(f"{'='*60}")

        while True:
            try:
                confirm = input("是否基于已有信息生成总结？(y/yes 确认，其他取消): ").strip().lower()
                if confirm in ['y', 'yes']:
                    print("✅ 正在生成总结...")
                    return await generate_partial_summary(user_input, exec_state, llm, logger, reason)
                else:
                    print("❌ 取消生成总结")
                    return f"{reason}，未完成分析。已获取 {len(exec_state.tool_calls)} 个工具的结果。"
            except (KeyboardInterrupt, EOFError):
                print("\n❌ 取消生成总结")
                return f"{reason}，未完成分析。已获取 {len(exec_state.tool_calls)} 个工具的结果。"

    async def _select_skills(self, user_input: str, catalog_prompt: str, llm, logger) -> list:
        """根据用户问题选择需要的技能"""
        prompt = SKILL_SELECTOR_PROMPT.format(skill_catalog=catalog_prompt)
        messages = [
            ("system", prompt),
            ("user", user_input),
        ]
        result = llm_json_with_retry(llm, messages, logger, label="skill-selector")
        if result and isinstance(result.get("selected_skills"), list):
            selected = result["selected_skills"]
            reason = result.get("reason", "")
            logger.info("R", f"选中技能: {selected}")
            if reason:
                logger.debug("R", f"选择理由: {reason}")
            return selected
        return []

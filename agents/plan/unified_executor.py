"""UnifiedPlan 统一执行步骤 — 一次性执行全部 plan step（编排层）"""

import json
import traceback

from langchain.agents import create_agent as create_react_agent

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.executor_callbacks import ExecutionState, ExecutionStateCallback
from agents.prompts import UNIFIED_EXECUTOR_SYSTEM
from agents.user_decision import ask_user_decision, format_step_status
from tools.skills import SkillPromptBuilder
from utils.budget import BudgetExceeded
from utils.llm_factory import TokenTracker, LLMDebugCallback
from utils.logger import ensure_radar


async def unified_execute_step(state: PlanExecute, ctx: AgentContext):
    """统一执行所有步骤（UnifiedPlan 模式 — 一次性执行全部）"""
    plan = state["plan"]
    logger = ensure_radar(ctx.logger)
    memory = ctx.memory
    registry = ctx.skill_registry
    llm = ctx.llm if hasattr(ctx, "llm") else None
    progress = ctx.progress_reporter
    budget = ctx.budget

    from utils.llm_factory import get_llm
    llm = get_llm()

    logger.phase("统一执行")
    if progress:
        progress.step_start(0, "统一执行全部步骤")

    all_tools = []
    step_strs = []
    for s in plan:
        skill = s.get("skill", "")
        if skill:
            tools = registry.get_tools(skill)
            if tools:
                all_tools.extend(tools)
        step_strs.append(f"  {s.get('step', '?')}. [{skill}] {s.get('instruction', '')}")
    plan_text = "\n".join(step_strs)

    if not all_tools:
        all_tools = registry.get_all_tools()
        logger.warn("U", "无匹配工具，加载全部")

    tool_desc_lines = []
    for t in all_tools:
        tool_desc_lines.append(f"- {t.name}: {t.description}")
    tool_desc = "\n".join(tool_desc_lines)

    history = memory.get_history()
    exec_messages = [
        ("system", f"""{UNIFIED_EXECUTOR_SYSTEM}

### 执行计划
{plan_text}

### 可用工具
{tool_desc}
"""),
        *history,
        ("user", state["input"]),
    ]

    try:
        budget.check(logger)
    except BudgetExceeded:
        past = state.get("past_steps", [])
        if past:
            summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
            return {"response": f"预算超限，已完成的结果:\n\n{summary}"}
        return {"response": "预算超限，未能获取有效结果"}

    try:
        agent = create_react_agent(llm, all_tools)
        exec_state = ExecutionState()
        callbacks = [
            TokenTracker(logger, "unified-executor", budget=budget),
            ExecutionStateCallback(exec_state, logger),
        ]

        if Config.LOG_LEVEL == "DEBUG":
            callbacks.append(LLMDebugCallback(logger, "unified-executor"))

        recorder = ctx.trace_recorder
        if recorder:
            callbacks.append(recorder)

        agent_resp = await agent.ainvoke(
            {"messages": exec_messages},
            config={
                "recursion_limit": Config.PLAN_EXECUTOR_TOOL_CALLS,
                "callbacks": callbacks,
            },
        )

        result_text = agent_resp["messages"][-1].content
        if not result_text or len(result_text.strip()) < 50:
            if exec_state.has_useful_results():
                from agents.executor_callbacks import generate_partial_summary
                result_text = await generate_partial_summary(
                    exec_state, "统一执行", "", llm, logger
                )
                result_text = "⚠️ 部分完成\n\n" + result_text
            else:
                raise ValueError(f"执行结果过短: {repr(result_text)}")

        past_results = [(f"统一执行", result_text)]
        memory.add_ai(f"统一执行结果: {result_text}")

        if progress:
            progress.step_complete(0, result_text[:100])

        return {
            "past_steps": past_results,
            "response": result_text,
        }

    except BudgetExceeded as e:
        logger.warning("U", f"预算超限: {e}")
        exec_state = locals().get("exec_state")
        if exec_state and exec_state.has_useful_results():
            from agents.executor_callbacks import generate_partial_summary
            result_text = await generate_partial_summary(
                exec_state, "统一执行", "", llm, logger
            )
            return {"response": f"⚠️ 部分完成（预算超限）\n\n{result_text}"}
        return {"response": f"分析因预算超限提前结束：{e}"}

    except Exception as e:
        logger.error("U", f"统一执行异常: {e} {traceback.format_exc()}")
        import traceback as tb
        past = state.get("past_steps", [])
        if past:
            summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
            return {"response": f"执行异常，但已有部分结果:\n\n{summary}"}
        status_lines, missing_lines = format_step_status(plan, [], 0)
        decision = ask_user_decision(
            header=f"执行失败：{e}",
            status_lines=status_lines + [""] + missing_lines,
            options=["基于已有数据生成总结", "放弃并返回错误信息"],
        )
        if "总结" in decision:
            return {"response": f"用户选择结束。未获取到有效结果。"}
        return {"response": f"执行失败: {tb.format_exc()}"}

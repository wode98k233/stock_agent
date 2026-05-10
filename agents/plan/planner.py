"""Planner 步骤函数 — 生成执行计划"""

import json

from langchain_core.prompts import ChatPromptTemplate

from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.prompts import PLAN_OUTPUT_FORMAT
from tools.skills import SkillPromptBuilder
from utils.llm_factory import get_llm, llm_json_with_retry
from utils.logger import ensure_radar


async def plan_step(state: PlanExecute, ctx: AgentContext):
    """根据分类结果生成详细的执行计划"""
    logger = ensure_radar(ctx.logger)
    memory = ctx.memory
    llm = get_llm()
    registry = ctx.skill_registry
    progress = ctx.progress_reporter

    logger.phase("生成执行计划")
    if progress:
        progress.step_start(0, "生成执行计划")

    history = memory.get_history()
    catalog_prompt = SkillPromptBuilder.build_catalog_prompt(registry)

    planner_messages = [
        ("system", f"""你是选股雷达的规划器 (Planner)。把用户的选股需求拆解为可执行的步骤序列。

{catalog_prompt}

{PLAN_OUTPUT_FORMAT}
"""),
        *history,
        ("user", "{input}"),
    ]

    result = llm_json_with_retry(
        llm,
        ChatPromptTemplate.from_messages(planner_messages).format_messages(input=state["input"]),
        logger,
        label="planner",
    )

    if result and "steps" in result:
        steps = result["steps"]
        constraints = result.get("constraints", [])

        logger.plan(steps, constraints)
        if progress:
            progress.planner_result(len(steps))

        return {
            "plan": steps,
            "current_step": 0,
            "user_constraints": ", ".join(constraints) if constraints else "",
            "key_data": {},
            "progress_reporter": progress,
        }

    return {"response": "抱歉，无法为您的需求生成执行计划，请尝试更具体的描述。"}

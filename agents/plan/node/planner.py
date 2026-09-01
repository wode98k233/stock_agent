"""Planner 步骤函数 — 生成执行计划"""

import json

from langchain_core.prompts import ChatPromptTemplate

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.prompts import PLAN_OUTPUT_FORMAT
from agents.shared.analysis_utils import limit_plan_steps
from agents.analysis.template_store import load_template, build_guidance
from tools.skills import SkillPromptBuilder
from utils.llm_factory import get_llm, llm_json_with_retry
from utils.logger import ensure_radar


def _escape_prompt_literal(text: str) -> str:
    """转义注入 prompt 的模板文本，避免其中占位符被 LangChain 解析。"""
    return (text or "").replace("{", "{{").replace("}", "}}")


async def plan_step(state: PlanExecute, ctx: AgentContext, run_config=None):
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

    template_guidance = ""
    template_id = state.get("template_id")
    if template_id:
        try:
            template = load_template(template_id)
            if template:
                template_guidance = _escape_prompt_literal(build_guidance(template, state["input"]))
        except Exception:
            pass

    planner_messages = [
        ("system", f"""你是选股雷达的规划器 (Planner)。把用户的选股需求拆解为可执行的步骤序列。

{catalog_prompt}

硬性要求：steps 最多只能输出 {Config.PLAN_MAX_STEPS} 步。

{PLAN_OUTPUT_FORMAT}"""),
        *history,
    ]
    if template_guidance:
        planner_messages.append(("system", template_guidance))
    planner_messages.append(("user", "{input}"))

    result = llm_json_with_retry(
        llm,
        ChatPromptTemplate.from_messages(planner_messages).format_messages(input=state["input"]),
        logger,
        label="planner",
        run_config=run_config,
    )

    if result and "steps" in result:
        steps = limit_plan_steps(result["steps"], logger=logger, tag="P")
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

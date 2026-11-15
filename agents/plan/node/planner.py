"""Planner 步骤函数 — 生成执行计划"""

import json

from langchain_core.prompts import ChatPromptTemplate

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.prompts import PLAN_OUTPUT_FORMAT
from agents.shared.analysis_utils import limit_plan_steps
from agents.shared.tool_utils import resolve_plan_step_skills
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

    # ⚠️ guidance 必须与内层子 Agent（common_react）**逐字节相同**，本调用才能像
    #    react 的 select_skills 那样给 agent 首调预热：
    #    ① 同一入参 user_input = state["input"]（内层见 graph.py::guidance_input）；
    #    ② 同一开关 include_report_structure=False（子步执行器只做维度、不含报告结构）。
    #    否则 messages[0] 与 agent 不同族 —— 实测 unified「planner → agent 共享 0」。
    guidance = ""
    template_id = state.get("template_id")
    if template_id:
        try:
            template = load_template(template_id)
            if template:
                # 不 escape（内层 graph.py 也不 escape）：保持一致才逐字节相同。
                guidance = build_guidance(template, state["input"],
                                          include_report_structure=False)
        except Exception:
            pass

    planner_tail = (
        "你是选股雷达的规划器 (Planner)。把用户的选股需求拆解为可执行的步骤序列。\n\n"
        f"{catalog_prompt}\n\n"
        f"硬性要求：steps 最多只能输出 {Config.PLAN_MAX_STEPS} 步。\n\n"
        f"{PLAN_OUTPUT_FORMAT}"
    )

    # 与 agent 家族**同形**：`[REACT][history][guidance][user]`（构造器已做头部 system 合并）
    # → 尾部追加规划指令。这样 messages[0] 与 agent 逐字节相同，agent 首调即可命中。
    # ⚠️ user 仍用**精简输入**（保留 6d7cae2 的省量）；与 agent 的 msg1（完整 input）在
    #    msg1 处才分叉 → 只热身到 msg0（tools + REACT + guidance ≈ 11k token）。
    #    取舍见 docs/memory/prompt-prefix-cache.md。
    from agents.base import get_lean_input
    from agents.shared.prompt_prefix import build_main_prefix_messages, append_tail_system
    messages = build_main_prefix_messages(history, guidance, get_lean_input(state["input"]))
    messages = append_tail_system(messages, planner_tail)

    # 绑定与内层 agent **同一份** tools（full 档下 = 全量 52，与技能无关）——
    # tools 在 payload 位置 0，不绑就与 agent 家族第 0 字节分叉。
    # registry 为 None（部分单测替身）时跳过，形状退化为无 tools（不影响功能）。
    from agents.shared.tool_utils import merge_tools_for_skills
    all_tools = []
    if registry is not None:
        all_tools = merge_tools_for_skills(
            registry, [], selected_skills=state.get("selected_skills", []),
            logger=logger, log_tag="P", fallback_skill="UnifiedPlan", for_binding=True)
    llm_call = llm
    if all_tools:
        try:
            llm_call = llm.bind_tools(all_tools)
        except Exception:  # 绑定失败不阻断规划：形状退化为无 tools
            llm_call = llm

    result = llm_json_with_retry(
        llm_call,
        messages,
        logger,
        label="planner",
        run_config=run_config,
    )

    if result and "steps" in result:
        steps = limit_plan_steps(result["steps"], logger=logger, tag="P")
        resolve_plan_step_skills(registry, steps, logger=logger, log_tag="P")
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

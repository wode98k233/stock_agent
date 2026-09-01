"""PDOR Classifier + Planner 节点"""
from agents.pdor.state import PdorState, PlanStep
from agents.agent_context import AgentContext
from agents.pdor.node.utils import _build_template_guidance
from agents.shared.analysis_utils import limit_plan_steps
from config import Config
from utils.logger import ensure_radar


# ── Classifier ──────────────────────────────────────────

async def classifier_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """快速分类：非股票问题直接回答，不走 planner（委托 shared ClassifyNode）"""
    from agents.shared.classify_node import make_classify_from_ctx

    classifier = make_classify_from_ctx(ctx)
    result = await classifier(state["input"], run_config=run_config)
    if not result["is_stock_related"]:
        return {"response": result["response"]}
    return {}


# ── Planner ─────────────────────────────────────────────

_PDOR_PLAN_PROMPT = """你是选股雷达的规划器。把用户需求拆解为可执行的步骤序列。

{catalog}

硬性要求：
- steps 最多只能输出 {max_steps} 步
- 所有步骤 type 统一为 "info"（信息收集），报告生成由系统自动完成
- 不允许超过 {max_steps} 步，超出部分会被截断丢弃

{template_guidance}

输出严格 JSON（不要其他文字）：
{{
  "steps": [
    {{"step": 1, "skill": "技能名", "purpose": "这一步的目的（一句话）", "type": "info"}},
    {{"step": 2, "skill": "技能名", "purpose": "目的", "type": "info"}}
  ],
  "constraints": ["约束条件"]
}}

规划原则：
1. 每步都是信息收集，专注于获取数据、搜索、计算，结果存入信息库
2. 不要规划"分析"或"总结"步骤，报告由系统自动生成
3. 每步只有 purpose（做什么），不要写 instruction（怎么做）
4. skill 必须从上面的技能目录中选择
5. 提取用户约束条件
6. 场景化数据契约中的硬性数据必须转化为步骤
"""


async def planner_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """生成执行计划"""
    from tools.skills import SkillPromptBuilder
    from utils.llm_factory import get_llm, llm_json_with_retry

    logger = ensure_radar(ctx.logger)
    llm = get_llm()
    registry = ctx.skill_registry
    progress = ctx.progress_reporter

    logger.phase("生成执行计划")
    if progress:
        progress.step_start(0, "生成执行计划")

    catalog = SkillPromptBuilder.build_catalog_prompt(registry)
    template_guidance = _build_template_guidance(state, logger)
    completed_summary = ""
    if state.get("_replan_count", 0) > 0:
        accumulated = state.get("info_accumulator", "").strip()
        if accumulated:
            completed_summary = "### 已完成步骤（请勿重复）\n" + accumulated
        else:
            completed = [s for s in state.get("plan_steps", []) if s.get("status") == "success"]
        if not accumulated and completed:
            lines = [f"Step {s['step']} [{s['skill']}]: {s['purpose']} — {s.get('result', '')}" for s in completed]
            completed_summary = "### 已完成步骤（请勿重复）\n" + "\n".join(lines)
    prompt = _PDOR_PLAN_PROMPT.format(
        catalog=catalog,
        template_guidance=template_guidance,
        max_steps=Config.PLAN_MAX_STEPS,
    )

    messages = [
        ("system", prompt),
    ]
    if completed_summary:
        messages.append(("system", completed_summary))
    messages.append(("user", state["input"]))

    result = llm_json_with_retry(llm, messages, logger, label="pdor-planner", run_config=run_config)

    if not result or "steps" not in result:
        return {"response": "无法生成执行计划，请尝试更具体的描述。"}

    steps = []
    for i, s in enumerate(result["steps"]):
        steps.append(PlanStep(
            step=i + 1,
            skill=s.get("skill", ""),
            purpose=s.get("purpose", ""),
            type=s.get("type", "info"),
            status="pending",
            result="",
            retry_count=0,
            executed_at="",
        ))

    steps = limit_plan_steps(steps, logger=logger, tag="P")
    constraints = ", ".join(result.get("constraints", []))

    updates = {
        "plan_steps": steps,
        "current_step_index": 0,
        "constraints": constraints,
    }

    # 首次规划时设置 original_plan
    if state.get("_replan_count", 0) == 0:
        updates["original_plan"] = list(steps)

    logger.info("P", f"计划: {len(steps)} 步")
    if progress:
        progress.planner_result(len(steps))

    return updates

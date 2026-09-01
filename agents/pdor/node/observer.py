"""PDOR Observer + Adjuster 节点"""
from agents.pdor.state import PdorState, PlanStep
from agents.agent_context import AgentContext
from agents.shared.analysis_utils import limit_plan_steps
from utils.logger import ensure_radar


# ── Observer ─────────────────────────────────────────────

_OBSERVER_CRITERIA = """你是一个执行观察器。根据完整计划和已执行结果，判定当前步骤的执行情况，决定下一步行动。

判定标准：
- success：步骤成功完成，继续执行下一步
- need_adjust：步骤有瑕疵，需要微调补充（如数据不完整、格式不对）
- need_replan：步骤失败严重影响后续计划，需要重新规划

注意：不要轻易判定 need_replan，除非失败确实导致后续步骤无法执行。

输出严格 JSON（不要其他文字）：
{{"observation": "success/need_adjust/need_replan", "reasoning": "判断理由"}}
"""

_OBSERVER_PLAN_TEMPLATE = """## 用户问题
{user_input}

## 执行计划（共 {total_steps} 步）
{plan_summary}
"""

_OBSERVER_CURRENT_STEP = """## 历史步骤结果
{history}

## 当前进度
执行到 Step {step_num}/{total_steps}，状态：{status}
"""


def _build_plan_summary(plan_steps: list) -> str:
    """构建静态计划摘要（不含状态，保持缓存稳定）"""
    lines = []
    for i, s in enumerate(plan_steps):
        num = s.get("step", i + 1)
        skill = s.get("skill", "")
        purpose = s.get("purpose", "")
        lines.append(f"Step {num} [{skill}] {purpose}")
    return "\n".join(lines)


def _build_history(plan_steps: list, current_idx: int) -> str:
    """构建历史步骤结果（放 user message 开头，前面部分不变可缓存）"""
    lines = []
    for i in range(current_idx+1):
        s = plan_steps[i]
        num = s.get("step", i + 1)
        result = s.get("result", "")
        lines.append(f"### Step {num} 结果\n{result}")
    return "\n\n".join(lines) if lines else "无（当前是第一步）"


async def observer_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """观察判定：success / need_adjust / need_replan"""
    logger = ensure_radar(ctx.logger)

    plan_steps = state.get("plan_steps", [])
    idx = state.get("current_step_index", 0)

    if idx >= len(plan_steps):
        return {"observation": "early_stop"}

    step = plan_steps[idx]
    status = step.get("status", "pending")
    replan_count = state.get("_replan_count", 0)

    # 重规划次数超限 → early_stop
    if replan_count >= 3:
        logger.info("O", f"重规划次数 ({replan_count}) >= 3，early_stop")
        return {"observation": "early_stop"}

    # 失败且重试耗尽 → need_replan（纯规则，无需 LLM）
    if status == "failed" and step.get("retry_count", 0) >= 2:
        logger.info("O", f"Step {step['step']} 失败且重试耗尽，need_replan")
        return {"observation": "need_replan", "_replan_count": replan_count + 1}

    # 其他情况（success / partial / 首次失败）→ LLM 判定
    from utils.llm_factory import get_llm, llm_json_with_retry
    llm = get_llm()

    plan_summary = _build_plan_summary(plan_steps)

    # 构建消息：system(判定标准) + 历史 + system(完整计划) + user(当前步骤)
    # 判定标准固定 → 可缓存；计划随步骤增长但前缀不变 → 部分可缓存
    messages = [
        ("system", _OBSERVER_CRITERIA),
    ]

    # 注入对话历史
    if ctx.memory and ctx.memory.enabled:
        history = ctx.memory.get_history()
        if history:
            messages.extend(history)

    messages.append(("system", _OBSERVER_PLAN_TEMPLATE.format(
            user_input=state.get("input", ""),
            total_steps=len(plan_steps),
            plan_summary=plan_summary,
        )))
    messages.append(("user", _OBSERVER_CURRENT_STEP.format(
            history=_build_history(plan_steps, idx),
            step_num=step["step"],
            total_steps=len(plan_steps),
            status=status,
        )))

    result = llm_json_with_retry(llm, messages, logger, label="pdor-observer", run_config=run_config, skip_cache_prefix=False)

    if not result:
        # LLM 失败，根据状态给默认值
        if status == "success":
            logger.info("O", f"LLM 失败，Step {step['step']} 成功，默认继续")
            return {"observation": "success", "current_step_index": idx + 1}
        return {"observation": "need_adjust"}

    obs = result.get("observation", "need_adjust")
    logger.info("O", f"Observer 判定: {obs} — {result.get('reasoning', '')}")

    updates = {"observation": obs}
    if obs == "success":
        updates["current_step_index"] = idx + 1
    elif obs == "need_replan":
        updates["_replan_count"] = replan_count + 1
    return updates


# ── Adjuster ─────────────────────────────────────────────

_ADJUSTER_PROMPT = """你是一个计划调整器。当前步骤执行出了问题，需要在当前步骤后插入 1-2 个补充步骤来弥补。
要求：
- 最多插入 2 个步骤
- 所有步骤 type 统一为 "info"（信息收集）
- 目的是弥补当前步骤的失败
输出 JSON：
{{
    "new_steps": [
        {{"skill": "技能名", "purpose": "补充步骤的目的", "type": "info"}}
    ],
    "reasoning": "调整理由"
}}

当前计划：
{plan_summary}

失败的步骤：Step {step_num} — {purpose}
失败原因：{result_summary}
"""


async def adjuster_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """调整计划：在当前步骤后插入补充步骤，整数重编号"""
    from utils.llm_factory import get_llm, llm_json_with_retry

    logger = ensure_radar(ctx.logger)
    llm = get_llm()

    plan_steps = state.get("plan_steps", [])
    idx = state.get("current_step_index", 0)

    if idx >= len(plan_steps):
        return {"plan_steps": plan_steps}

    step = plan_steps[idx]
    plan_summary = "\n".join([f"  {s['step']}. [{s['skill']}] {s['purpose']} ({s['status']})" for s in plan_steps])

    messages = [
        ("system", "你是一个计划调整器。只输出 JSON。"),
        ("user", _ADJUSTER_PROMPT.format(
            plan_summary=plan_summary,
            step_num=step["step"],
            purpose=step["purpose"],
            result_summary=step.get("result", ""),
        )),
    ]

    result = llm_json_with_retry(llm, messages, logger, label="pdor-adjuster", run_config=run_config, skip_cache_prefix=True)

    if not result or not result.get("new_steps"):
        logger.info("A", "Adjuster 无调整建议")
        return {"plan_steps": plan_steps, "current_step_index": idx + 1}

    # 插入新步骤
    new_steps_raw = result["new_steps"][:2]  # 最多 2 个
    insert_pos = idx + 1

    new_plan = list(plan_steps)
    for ns in new_steps_raw:
        new_plan.insert(insert_pos, PlanStep(
            step=0,  # 稍后重编号
            skill=ns.get("skill", ""),
            purpose=ns.get("purpose", ""),
            type=ns.get("type", "info"),
            status="pending",
            result="",
            retry_count=0,
            executed_at="",
        ))
        insert_pos += 1

    # 整数重编号
    for i, s in enumerate(new_plan):
        s["step"] = i + 1

    new_plan = limit_plan_steps(new_plan, logger=logger, tag="A")

    logger.info("A", f"插入 {len(new_steps_raw)} 个补充步骤，计划变为 {len(new_plan)} 步")
    return {"plan_steps": new_plan, "current_step_index": idx + 1}

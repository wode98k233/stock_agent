"""Observer 模块 — 每步执行后快速判断 pass/adjust/replan

快速通道：success → 直接 pass，~0 tokens
LLM 通道：异常/失败 → LLM 判断 pass/adjust/replan，~1.5K tokens

Observer 结果汇入最终总结，增加可解释性。
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class ObserverResult:
    """Observer 决策结果"""
    decision: str = "pass"          # "pass" | "adjust" | "replan"
    reasoning: str = ""             # 推理过程（汇入最终总结）
    adjust_suggestions: Optional[List[dict]] = None  # adjust 时的具体建议
    failure_impact: str = "无影响"  # 失败对后续步骤的影响

    def to_dict(self) -> dict:
        return asdict(self)


OBSERVER_PROMPT = """你是一个执行观察器。根据当前计划和刚执行完的步骤结果，决定下一步行动。

当前计划：
{plan_summary}

刚执行完的步骤（Step {step_idx}）：
- 目的：{step_purpose}
- 技能：{skill_name}
- 状态：{status}
- 结果摘要：
{result_summary}

历史执行情况：
{execution_history}

请判断：
1. 这个步骤是否成功完成了它的目的？
2. 如果失败/部分完成，是否影响后续步骤的可行性？
3. 失败的步骤占总步骤的比例是多少？

输出 JSON：
{{
    "decision": "pass",
    "reasoning": "步骤成功获取了所需数据，可以继续执行下一步",
    "adjust_suggestions": null,
    "failure_impact": "无影响"
}}

decision 取值：
- "pass"：步骤成功或瑕疵不影响大局，继续执行下一步
- "adjust"：步骤有瑕疵，需要微调 1-2 个后续步骤（在 adjust_suggestions 中说明）
- "replan"：步骤失败严重影响后续计划，需要重新规划（在 failure_impact 中说明原因）

adjust_suggestions 格式（仅 adjust 时需要）：
[{{"step_idx": 2, "new_skill": "mx_data", "new_instruction": "修改后的指令", "reason": "修改原因"}}]"""


def should_run_observer(step_result: dict, budget_status: dict) -> bool:
    """判断是否需要运行 Observer

    快速通道（不调 LLM）：success → 直接 pass
    LLM 通道：partial/fail → 需要 LLM 判断
    跳过：预算太紧张（>95%）
    """
    # 成功步骤走快速通道，不触发 LLM
    if step_result.get("status") == "success":
        return False

    # 预算太紧张时跳过
    pct = budget_status.get("tokens_percent", 0)
    if pct > 95:
        return False

    return True


def fast_path_result(step_result: dict) -> ObserverResult:
    """快速通道：成功步骤直接返回 pass"""
    return ObserverResult(
        decision="pass",
        reasoning=f"步骤成功完成（{step_result.get('skill', '?')}）",
        adjust_suggestions=None,
        failure_impact="无影响",
    )


async def observe_step(
    plan: list,
    step_idx: int,
    step_result: dict,
    all_step_results: list,
    llm,
    logger,
) -> ObserverResult:
    """Observer 主入口

    快速通道：status == success → pass（~0 tokens）
    LLM 通道：其他情况 → LLM 判断 pass/adjust/replan（~1.5K tokens）
    """
    # 快速通道
    if step_result.get("status") == "success":
        return fast_path_result(step_result)

    # LLM 通道
    try:
        return await _llm_observe(plan, step_idx, step_result, all_step_results, llm, logger)
    except Exception as e:
        logger.warning("O", f"Observer LLM 调用失败，默认 pass: {e}")
        return ObserverResult(
            decision="pass",
            reasoning=f"Observer 调用失败，默认继续: {str(e)[:100]}",
        )


async def _llm_observe(
    plan: list,
    step_idx: int,
    step_result: dict,
    all_step_results: list,
    llm,
    logger,
) -> ObserverResult:
    """LLM 通道 — 调用 LLM 判断"""
    from utils.llm_factory import llm_json_with_retry

    # 构建计划摘要
    plan_summary = "\n".join([
        f"  {s.get('step', i+1)}. [{s.get('skill', '')}] {s.get('purpose', '')}"
        for i, s in enumerate(plan)
    ])

    # 构建执行历史（最近 3 步）
    recent = all_step_results[-3:] if len(all_step_results) > 3 else all_step_results
    execution_history = "\n".join([
        f"  Step {sr.get('step_idx', '?')+1} [{sr.get('skill', '?')}]: "
        f"[{sr.get('status', '?')}] {sr.get('summary', '')[:200]}"
        for sr in recent
    ]) if recent else "  无历史记录"

    # 当前步骤信息
    status = step_result.get("status", "unknown")
    result_summary = step_result.get("summary", "")[:500]
    skill_name = step_result.get("skill", "unknown")
    step_purpose = ""
    if step_idx < len(plan):
        step_purpose = plan[step_idx].get("purpose", "")

    prompt = OBSERVER_PROMPT.format(
        plan_summary=plan_summary,
        step_idx=step_idx + 1,
        step_purpose=step_purpose,
        skill_name=skill_name,
        status=status,
        result_summary=result_summary,
        execution_history=execution_history,
    )

    messages = [
        ("system", "你是一个执行观察器，负责判断步骤执行结果并决定下一步行动。只输出 JSON。"),
        ("user", prompt),
    ]

    result = llm_json_with_retry(llm, messages, logger, label="observer")

    if not result:
        return ObserverResult(
            decision="pass",
            reasoning="Observer 返回空结果，默认继续",
        )

    return ObserverResult(
        decision=result.get("decision", "pass"),
        reasoning=result.get("reasoning", ""),
        adjust_suggestions=result.get("adjust_suggestions"),
        failure_impact=result.get("failure_impact", "无影响"),
    )

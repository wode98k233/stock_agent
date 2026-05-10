"""Replanner 步骤函数 — 重新规划（编排层）

保留原 replan_step 的编排逻辑，分析逻辑委托给 analyze.py。
"""

import json

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.prompts import REPLAN_OUTPUT_FORMAT
from agents.plan.analyze import check_termination, detect_loop, dedup_new_steps
from agents.plan.handler import handle_budget_exceeded
from tools.skills import SkillPromptBuilder
from utils.budget import BudgetExceeded
from utils.llm_factory import get_llm, llm_json_with_retry
from utils.logger import ensure_radar

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, BaseMessage

def _build_budget_status(budget) -> str:
    """构建预算状态文本"""
    try:
        status = budget.get_status()
        tokens_pct = status.get("tokens_percent", 0)
        return f"""⚠️ 预算状态：
- 已使用 tokens: {status.get('tokens', 0)} / {status.get('tokens_limit', 50000)} ({tokens_pct}%)
- 已使用 LLM 调用: {status.get('calls', 0)} / {status.get('calls_limit', 30)}
- 已用时间: {status.get('elapsed_seconds', 0)}s / {status.get('time_limit', 60)}s

预算约束（必须遵守）：
- 消耗超过 60%：不要追加新步骤，考虑基于已有数据生成回答
- 消耗超过 80%：必须立即返回 respond"""
    except Exception:
        return ""

def _to_messages(items):
    """将 (role, content) 元组或 BaseMessage 统一转为消息对象，避免模板转义"""
    msgs = []
    for item in items:
        if isinstance(item, BaseMessage):
            msgs.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            role, content = item
            if role == "system":
                msgs.append(SystemMessage(content=content))
            elif role == "user":
                msgs.append(HumanMessage(content=content))
            elif role in ("ai", "assistant"):
                msgs.append(AIMessage(content=content))
            else:
                msgs.append(HumanMessage(content=content))   # fallback
        else:
            msgs.append(HumanMessage(content=str(item)))
    return msgs

async def replan_step(state: PlanExecute, ctx: AgentContext):
    """执行后重新规划 — 决定继续、结束或调整计划"""
    logger = ensure_radar(ctx.logger)
    memory = ctx.memory
    llm = get_llm()
    registry = ctx.skill_registry
    progress = ctx.progress_reporter
    budget = ctx.budget

    plan = state["plan"]
    past_steps = state.get("past_steps", [])
    key_data = state.get("key_data", {})
    step_results = state.get("step_results", [])
    current_step = state.get("current_step", 0)
    original_plan_len = len(plan)

    logger.phase("重新规划")

    should_end, reason = check_termination(state, original_plan_len)
    if should_end:
        if progress:
            progress.final()
        # 优先用 step_results（完整数据），回退到 past_steps
        if step_results:
            status_icons = {"success": "✅", "partial": "⚠️", "fail": "❌", "tool_unavailable": "🚫"}
            past_summary = "\n\n".join([
                f"### {status_icons.get(sr.get('status', ''), '✅')} Step {sr.get('step_idx', '?')+1}: {sr.get('skill', '?')}"
                + (f"\n> Observer: {sr.get('observer_decision', '')} — {sr.get('observer_reasoning', '')}" if sr.get('observer_decision') else "")
                + f"\n\n{sr.get('summary', '')}"
                for sr in step_results
            ])
        else:
            past_summary = "\n\n".join([f"- {desc}:\n{res}" for desc, res in past_steps])
        return {"response": f"所有步骤已完成。\n\n{past_summary}"}

    try:
        budget.check(logger)
    except BudgetExceeded:
        result = handle_budget_exceeded(state, logger)
        if result.get("_continue"):
            logger.info("B", "用户选择继续执行，跳过本次 replan")
            return {"current_step": state.get("current_step", 0)}
        return result

    if progress:
        progress.milestone(f"已完成 {len(past_steps)} 步，当前步骤 {current_step}/{original_plan_len}")

    history = memory.get_history()
    catalog_prompt = SkillPromptBuilder.build_catalog_prompt(registry)

    step_status = format_step_status(plan, past_steps, current_step, step_results)

    # 优先从 step_results 构建摘要，回退到 past_steps
    if step_results:
        completed_so_far = "\n".join([
            f"- Step {sr.get('step_idx', '?')} [{sr.get('skill', '?')}]: "
            f"[{sr.get('status', '?')}] {sr.get('summary', '')[:800]}"
            for sr in step_results
        ])
    else:
        completed_so_far = "\n".join([f"- {desc}: {res[:800]}" for desc, res in past_steps])

    # 预算状态注入
    budget_status = _build_budget_status(budget)

    # 标记 tool_unavailable 的步骤类型
    unavailable_skills = set()
    for sr in step_results:
        if sr.get("tool_unavailable"):
            unavailable_skills.add(sr.get("skill", ""))
    unavailable_block = ""
    if unavailable_skills:
        unavailable_block = f"\n\n⚠️ 以下技能的工具不可用，不要再安排：{', '.join(unavailable_skills)}"

    user_constraints = state.get("user_constraints", "")
    constraints_block = f"\n\n[用户约束] {user_constraints}" if user_constraints else ""

    # 检查 Observer 决策
    last_observer = step_results[-1] if step_results else {}
    observer_decision = last_observer.get("observer_decision", "")
    observer_reasoning = last_observer.get("observer_reasoning", "")
    adjust_suggestions = last_observer.get("adjust_suggestions") or []

    # Observer adjust：直接应用建议，不调 LLM
    if observer_decision == "adjust" and adjust_suggestions:
        logger.info("R", f"Observer 建议 adjust，应用 {len(adjust_suggestions)} 个调整")
        merged_plan = list(plan)
        applied = 0
        for suggestion in adjust_suggestions[:2]:  # 最多调整 2 个步骤
            target_idx = suggestion.get("step_idx", 0) - 1  # 1-indexed → 0-indexed
            if 0 <= target_idx < len(merged_plan) and target_idx >= current_step:
                if suggestion.get("new_instruction"):
                    merged_plan[target_idx]["instruction"] = suggestion["new_instruction"]
                if suggestion.get("new_skill"):
                    merged_plan[target_idx]["skill"] = suggestion["new_skill"]
                applied += 1
                logger.info("R", f"  调整 Step {target_idx+1}: {suggestion.get('reason', '')}")
        if applied > 0:
            return {
                "plan": merged_plan,
                "_replan_history": state.get("_replan_history", []) + ["adjust"],
            }

    # Observer replan：注入失败影响到 prompt
    observer_block = ""
    if observer_decision == "replan":
        observer_block = f"\n\n⚠️ Observer 建议重新规划：{observer_reasoning}\n失败影响：{last_observer.get('failure_impact', '未知')}"

    replan_messages = [
        ("system", f"""你是选股雷达的重新规划器 (Replanner)。
检查当前执行进度并决定下一步。

{budget_status}

现有技能：
{catalog_prompt}

执行状态：
{step_status}

已完成步骤的结果摘要：
{completed_so_far}

关键数据汇总：
{json.dumps(key_data, ensure_ascii=False, indent=2)}
{unavailable_block}
{observer_block}
{constraints_block}
{REPLAN_OUTPUT_FORMAT}
"""),
        *history,
        ("user", f"原始问题：{state['input']}"),
    ]

    messages = _to_messages(replan_messages)
    result = llm_json_with_retry(
        llm,
        messages,
        logger,
        label="replanner",
    )

    if not result:
        logger.warning("R", "Replanner 返回空结果，自动结束")
        if step_results:
            past_summary = "\n\n".join([
                f"### Step {sr.get('step_idx', '?')+1}: {sr.get('skill', '?')}\n{sr.get('summary', '')}"
                for sr in step_results
            ])
        else:
            past_summary = "\n\n".join([f"- {desc}:\n{res}" for desc, res in past_steps])
        return {"response": f"分析完成。\n\n{past_summary}"}

    action = result.get("action", "continue")
    response_text = result.get("response", "")

    if action == "respond" and response_text:
        if progress:
            progress.final()
        return {"response": response_text}

    new_steps = result.get("steps", result.get("plan", []))

    if detect_loop(state.get("_replan_history", []), action, new_steps):
        logger.warning("R", "检测到重新规划循环，触发用户决策")
        from agents.user_decision import ask_user_decision

        decision = ask_user_decision(
            header="⚠️ 检测到重新规划循环（连续多次重新规划结果相同）",
            status_lines=format_step_status(plan, past_steps, current_step, step_results).split("\n"),
            options=["基于已有结果输出最终回答", "忽略循环，继续执行"],
        )

        if "继续" in decision:
            logger.info("R", "用户选择忽略循环，继续执行")
            state["_replan_loop_count"] = state.get("_replan_loop_count", 0) + 1
        else:
            if progress:
                progress.final()
            if step_results:
                past_summary = "\n\n".join([
                    f"### Step {sr.get('step_idx', '?')+1}: {sr.get('skill', '?')}\n{sr.get('summary', '')}"
                    for sr in step_results
                ])
            else:
                past_summary = "\n\n".join([f"- {desc}:\n{res}" for desc, res in past_steps])
            return {"response": f"以下是根据已有信息生成的回答：\n\n{past_summary}"}

    if new_steps:
        unique_new_steps, skipped, replace_map = dedup_new_steps(new_steps, past_steps, plan, current_step)

        if skipped:
            logger.info("R", f"去重过滤：{skipped}")

        if unique_new_steps:
            # 替换语义：replace_map 中的步骤替换老步骤，其余追加到末尾
            merged_plan = list(plan)  # 浅拷贝，避免修改原 plan

            # 先执行替换
            replaced_count = 0
            append_steps = []
            for i, step in enumerate(unique_new_steps):
                if i in replace_map:
                    old_idx = replace_map[i]
                    step["step"] = old_idx + 1  # 保持原位置编号
                    merged_plan[old_idx] = step
                    replaced_count += 1
                else:
                    append_steps.append(step)

            # 追加全新步骤，编号从 max(原编号) + 1 开始
            max_step = max((s.get("step", j+1) for j, s in enumerate(merged_plan)), default=0)
            for step in append_steps:
                max_step += 1
                step["step"] = max_step
                merged_plan.append(step)

            logger.info("R", f"合并后的计划：{len(merged_plan)} 步（替换 {replaced_count} + 追加 {len(append_steps)}）")

            if progress:
                progress.replanner_result(
                    replaced_count + len(append_steps),
                    [s.get("purpose", "") for s in unique_new_steps],
                )

            return {
                "plan": merged_plan,
                "_replan_history": state.get("_replan_history", []) + [action],
            }

    logger.info("R", "Replanner 没有新的规划，继续执行")
    if progress:
        progress.replanner_result(0, [])

    return {"current_step": state.get("current_step", 0)}


def format_step_status(plan: list, past_steps: list, current_step: int, step_results=None) -> str:
    """格式化步骤状态 — 优先从 step_results 读取状态"""
    status_icons = {"success": "✅", "partial": "⚠️", "fail": "❌", "tool_unavailable": "🚫"}

    # 构建 step_idx → status 的映射
    result_status = {}
    if step_results:
        for sr in step_results:
            idx = sr.get("step_idx")
            if idx is not None:
                result_status[idx] = sr.get("status", "success")

    completed = {desc.split(":")[0].strip() for desc, _ in past_steps}
    lines = []
    for i, step in enumerate(plan):
        step_label = f"Step {step.get('step', i+1)}"
        purpose = step.get('purpose', '')

        if i in result_status:
            # 从 step_results 读取状态
            st = result_status[i]
            icon = status_icons.get(st, "✅")
            suffix = "（工具不可用）" if st == "tool_unavailable" else ""
            lines.append(f"  {icon} {step_label}: {purpose}{suffix}")
        elif step_label in completed or i < current_step:
            lines.append(f"  ✅ {step_label}: {purpose}")
        elif i == current_step:
            lines.append(f"  🔄 {step_label}: {purpose}（当前执行中）")
        elif i < len(plan):
            lines.append(f"  ⬜ {step_label}: {purpose}")
    return "\n".join(lines)

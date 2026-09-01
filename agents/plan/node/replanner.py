"""Replanner 步骤函数 — 重新规划（编排层）

职责拆分：
- replan_step: 主入口，编排终止检查 → Observer 处理 → LLM 调用 → 计划合并
- _handle_observer_decision: 处理 Observer 的 adjust/replan 建议
- _build_replan_prompt: 构建 replanner 的 LLM 提示词
- _apply_new_steps: 将新步骤合并到现有计划
- _build_completed_summary: 从 step_results 或 past_steps 构建摘要
"""

import json

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.prompts import REPLAN_OUTPUT_FORMAT
from agents.plan.analyze import check_termination, detect_loop, dedup_new_steps
from agents.shared.analysis_utils import limit_plan_steps
from agents.shared.exception_utils import handle_budget_exceeded
from agents.analysis.template_store import load_template
from tools.skills import SkillPromptBuilder
from utils.budget import BudgetExceeded, BudgetExemptWindow
from utils.llm_factory import get_llm, llm_json_with_retry
from utils.logger import ensure_radar

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, BaseMessage


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

def _to_messages(items):
    """将 (role, content) 元组或 BaseMessage 统一转为消息对象"""
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
                msgs.append(HumanMessage(content=content))
        else:
            msgs.append(HumanMessage(content=str(item)))
    return msgs


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


def _build_completed_summary(step_results, past_steps):
    """从 step_results 或 past_steps 构建已完成步骤摘要"""
    if step_results:
        return "\n".join([
            f"- Step {sr.get('step_idx', '?')} [{sr.get('skill', '?')}]: "
            f"[{sr.get('status', '?')}] {sr.get('summary', '')[:800]}"
            for sr in step_results
        ])
    return "\n".join([f"- {desc}: {res[:800]}" for desc, res in past_steps])


def _build_final_response(step_results, past_steps):
    """构建最终响应（所有步骤完成时）"""
    if step_results:
        status_icons = {"success": "✅", "partial": "⚠️", "fail": "❌", "tool_unavailable": "🚫"}
        return "\n\n".join([
            f"### {status_icons.get(sr.get('status', ''), '✅')} Step {sr.get('step_idx', '?')+1}: {sr.get('skill', '?')}"
            + (f"\n> Observer: {sr.get('observer_decision', '')} — {sr.get('observer_reasoning', '')}" if sr.get('observer_decision') else "")
            + f"\n\n{sr.get('summary', '')}"
            for sr in step_results
        ])
    return "\n\n".join([f"- {desc}:\n{res}" for desc, res in past_steps])


def _load_qa_rules(template_id):
    """加载模板的 QA 规则"""
    if not template_id:
        return ""
    try:
        template = load_template(template_id)
        if template and template.get("qa_rules"):
            rules = template["qa_rules"]
            if rules:
                return "\n## 质量门禁（必须遵守）\n" + "\n".join(
                    f"- {r}" if isinstance(r, str) else f"- {r.get('rule', r)}" for r in rules
                )
    except Exception:
        pass
    return ""


# ══════════════════════════════════════════════════════════════
# Observer 决策处理
# ══════════════════════════════════════════════════════════════

def _handle_observer_adjust(step_results, plan, current_step, logger):
    """处理 Observer 的 adjust 建议 — 直接应用，不调 LLM。

    返回 merged_plan 或 None（表示无需调整）。
    """
    if not step_results:
        return None
    last = step_results[-1]
    if last.get("observer_decision") != "adjust":
        return None
    suggestions = last.get("adjust_suggestions") or []
    if not suggestions:
        return None

    logger.info("R", f"Observer 建议 adjust，应用 {len(suggestions)} 个调整")
    merged_plan = list(plan)
    applied = 0
    for suggestion in suggestions[:2]:
        target_idx = suggestion.get("step_idx", 0) - 1
        if 0 <= target_idx < len(merged_plan) and target_idx >= current_step:
            if suggestion.get("new_instruction"):
                merged_plan[target_idx]["instruction"] = suggestion["new_instruction"]
            if suggestion.get("new_skill"):
                merged_plan[target_idx]["skill"] = suggestion["new_skill"]
            applied += 1
            logger.info("R", f"  调整 Step {target_idx+1}: {suggestion.get('reason', '')}")
    return merged_plan if applied > 0 else None


def _build_observer_block(step_results):
    """构建 Observer replan 提示块"""
    if not step_results:
        return ""
    last = step_results[-1]
    if last.get("observer_decision") != "replan":
        return ""
    reasoning = last.get("observer_reasoning", "")
    impact = last.get("failure_impact", "未知")
    return f"\n\n⚠️ Observer 建议重新规划：{reasoning}\n失败影响：{impact}"


# ══════════════════════════════════════════════════════════════
# 提示词构建
# ══════════════════════════════════════════════════════════════

def _build_replan_prompt(state, plan, past_steps, step_results, current_step,
                         registry, budget, logger):
    """构建 replanner 的完整提示词消息列表"""
    catalog_prompt = SkillPromptBuilder.build_catalog_prompt(registry)
    step_status = format_step_status(plan, past_steps, current_step, step_results)
    budget_status = _build_budget_status(budget)
    observer_block = _build_observer_block(step_results)
    qa_rules_text = _load_qa_rules(state.get("template_id"))

    # 不可用技能
    unavailable_skills = {sr.get("skill", "") for sr in step_results if sr.get("tool_unavailable")}
    unavailable_block = ""
    if unavailable_skills:
        unavailable_block = f"\n\n⚠️ 以下技能的工具不可用，不要再安排：{', '.join(unavailable_skills)}"

    user_constraints = state.get("user_constraints", "")
    constraints_block = f"\n\n[用户约束] {user_constraints}" if user_constraints else ""

    # 静态内容放 system（可缓存），动态内容放 user（每次不同）
    return [
        ("system", f"""你是选股雷达的重新规划器 (Replanner)。
检查当前执行进度并决定下一步。

现有技能：
{catalog_prompt}

{qa_rules_text}
{REPLAN_OUTPUT_FORMAT}
"""),
        *state.get("_history", []),
        ("user", f"""原始问题：{state['input']}

执行状态：
{step_status}
{unavailable_block}
{observer_block}
{constraints_block}

{budget_status}"""),
    ]


# ══════════════════════════════════════════════════════════════
# 计划合并
# ══════════════════════════════════════════════════════════════

def _apply_new_steps(new_steps, plan, past_steps, current_step, step_results, logger):
    """将新步骤合并到现有计划。返回 (merged_plan, replan_history_entry) 或 None。"""
    unique_new_steps, skipped, replace_map = dedup_new_steps(new_steps, past_steps, plan, current_step)
    if skipped:
        logger.info("R", f"去重过滤：{skipped}")
    if not unique_new_steps:
        return None

    merged_plan = list(plan)
    replaced_count = 0
    append_steps = []

    for i, step in enumerate(unique_new_steps):
        if i in replace_map:
            old_idx = replace_map[i]
            step["step"] = old_idx + 1
            merged_plan[old_idx] = step
            replaced_count += 1
        else:
            append_steps.append(step)

    max_step = max((s.get("step", j+1) for j, s in enumerate(merged_plan)), default=0)
    for step in append_steps:
        max_step += 1
        step["step"] = max_step
        merged_plan.append(step)

    merged_plan = limit_plan_steps(merged_plan, logger=logger, tag="R")
    logger.info("R", f"合并后的计划：{len(merged_plan)} 步（替换 {replaced_count} + 追加 {len(append_steps)}）")
    return merged_plan


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

async def replan_step(state: PlanExecute, ctx: AgentContext, run_config=None):
    """执行后重新规划 — 决定继续、结束或调整计划"""
    logger = ensure_radar(ctx.logger)
    memory = ctx.memory
    llm = get_llm()
    registry = ctx.skill_registry
    progress = ctx.progress_reporter
    budget = ctx.budget

    plan = state["plan"]
    past_steps = state.get("past_steps", [])
    step_results = state.get("step_results", [])
    current_step = state.get("current_step", 0)
    original_plan_len = len(plan)

    logger.phase("重新规划")

    # 1. 终止检查
    should_end, reason = check_termination(state, original_plan_len)
    if should_end:
        if progress:
            progress.final()
        past_summary = _build_final_response(step_results, past_steps)
        qa_suffix = ""
        template_id = state.get("template_id")
        if template_id:
            try:
                template = load_template(template_id)
                if template and template.get("qa_rules"):
                    rules = template["qa_rules"]
                    if rules:
                        qa_lines = "\n".join(f"- {r}" if isinstance(r, str) else f"- {r.get('rule', r)}" for r in rules)
                        qa_suffix = f"\n\n---\n**质量门禁检查：**\n{qa_lines}"
            except Exception:
                pass
        return {"response": f"所有步骤已完成。\n\n{past_summary}{qa_suffix}"}

    # 2. 预算检查
    try:
        budget.check(logger)
    except BudgetExceeded:
        result = handle_budget_exceeded(state, logger)
        if result.get("_user_approved_overrun"):
            exempt = BudgetExemptWindow(
                calls_limit=Config.BUDGET_EXEMPT_CALLS_LIMIT,
                time_limit=Config.BUDGET_EXEMPT_TIME_LIMIT,
            )
            budget.set_exempt_window(exempt)
            logger.info("B", "用户选择继续执行，创建豁免窗口，跳过本次 replan")
            return {"current_step": current_step, "budget_exempt": exempt.to_dict()}
        return result

    if progress:
        progress.milestone(f"已完成 {len(past_steps)} 步，当前步骤 {current_step}/{original_plan_len}")

    # 3. Observer adjust — 直接应用，不调 LLM
    adjusted_plan = _handle_observer_adjust(step_results, plan, current_step, logger)
    if adjusted_plan is not None:
        return {"plan": adjusted_plan, "_replan_history": state.get("_replan_history", []) + ["adjust"]}

    # 4. 调 LLM 重新规划
    history = memory.get_history()
    state_with_history = {**state, "_history": history}
    replan_messages = _build_replan_prompt(state_with_history, plan, past_steps, step_results, current_step, registry, budget, logger)

    messages = _to_messages(replan_messages)
    result = llm_json_with_retry(llm, messages, logger, label="replanner", run_config=run_config)

    if not result:
        logger.warning("R", "Replanner 返回空结果，自动结束")
        past_summary = _build_final_response(step_results, past_steps)
        return {"response": f"分析完成。\n\n{past_summary}"}

    action = result.get("action", "continue")
    response_text = result.get("response", "")
    if isinstance(response_text, dict):
        response_text = json.dumps(response_text, ensure_ascii=False)

    # 5. respond — 直接返回
    if action == "respond" and response_text:
        if progress:
            progress.final()
        return {"response": response_text}

    # 6. continue — 合并新步骤
    new_steps = result.get("steps", result.get("plan", []))

    # 循环检测
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
            past_summary = _build_final_response(step_results, past_steps)
            return {"response": f"以下是根据已有信息生成的回答：\n\n{past_summary}"}

    if new_steps:
        merged = _apply_new_steps(new_steps, plan, past_steps, current_step, step_results, logger)
        if merged is not None:
            if progress:
                new_count = len(merged) - len(plan) + len(plan)  # approximate
                progress.replanner_result(f"计划更新：{len(merged)} 步")
            return {"plan": merged, "_replan_history": state.get("_replan_history", []) + [action]}

    logger.info("R", "Replanner 没有新的规划，继续执行")
    if progress:
        progress.replanner_result("无新规划")
    return {"current_step": current_step}


def format_step_status(plan: list, past_steps: list, current_step: int, step_results=None) -> str:
    """格式化步骤状态 — 优先从 step_results 读取状态"""
    status_icons = {"success": "✅", "partial": "⚠️", "fail": "❌", "tool_unavailable": "🚫"}

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

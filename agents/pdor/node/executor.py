"""PDOR Executor 节点 — 执行当前计划步骤"""
import asyncio
import datetime

from agents.pdor.state import PdorState, PlanStep
from agents.agent_context import AgentContext
from agents.pdor.node.utils import _tool_results_to_tool_calls
from agents.shared.step_utils import restore_exempt_window, create_exempt_window, ErrorDeduplicator
from config import Config
from utils.budget import BudgetExceeded
from utils.logger import ensure_radar


async def executor_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """执行当前步骤"""
    from utils.llm_factory import get_llm
    from agents.shared.exception_utils import handle_budget_exceeded

    logger = ensure_radar(ctx.logger)
    llm = get_llm()
    budget = ctx.budget
    progress = ctx.progress_reporter

    plan_steps = state.get("plan_steps", [])
    idx = state.get("current_step_index", 0)

    # Guard: index 越界
    if idx >= len(plan_steps):
        return {"observation": "early_stop"}

    step = plan_steps[idx]
    step_num = step["step"]
    skill = step["skill"]
    purpose = step["purpose"]

    logger.phase("执行步骤")
    logger.step_start(step_num, skill, purpose, purpose)
    if progress:
        progress.step_start(step_num, f"[{skill}] {purpose}")

    # 恢复豁免窗口
    exempt_window = restore_exempt_window(state)

    error_dedup = ErrorDeduplicator(strategy="last")
    failed_tools = list(state.get("_failed_tools", []) or [])
    max_retries = Config.PLAN_EXECUTOR_MAX_RETRIES
    for attempt in range(max_retries):
        try:
            # 预算检查
            if exempt_window and exempt_window.is_active():
                exempt_window.consume()
                logger.info("E", f"豁免窗口内，跳过 budget check（剩余 {exempt_window.remaining_calls} 次）")
            else:
                budget.check(logger)

            # 统一走 ReAct 工具调用（所有 step 都是 info 类型）
            result_text, tool_results = await _run_react_step(step, state, ctx, llm, logger, failed_tools=failed_tools or None)

            # 成功
            now = datetime.datetime.now().isoformat()
            new_steps = list(plan_steps)
            new_steps[idx] = PlanStep(
                step=step_num, skill=skill, purpose=purpose, type=step["type"],
                status="success", result=result_text, retry_count=attempt, executed_at=now,
            )

            # step 结果和 tool_calls 累积到 state
            evidence = f"### Step {step_num}: {purpose}\n{result_text}"
            accumulated = state.get("info_accumulator", "").strip()
            updates = {
                "plan_steps": new_steps,
                "info_accumulator": (
                    f"{accumulated}\n\n{evidence}" if accumulated else evidence
                ),
            }
            new_tool_calls = _tool_results_to_tool_calls(tool_results)
            if new_tool_calls:
                updates["tool_calls"] = list(state.get("tool_calls", [])) + new_tool_calls

            logger.step_end(step_num, success=True, summary=result_text)
            if progress:
                progress.step_complete(step_num, result_text)

            return updates

        except BudgetExceeded:
            decision = handle_budget_exceeded(state, logger)
            if decision.get("_user_approved_overrun"):
                exempt_window = create_exempt_window(budget)
                state["budget_exempt"] = exempt_window.to_dict()
                continue
            else:
                return {"observation": "early_stop"}

        except Exception as e:
            if error_dedup.is_duplicate(e):
                logger.error("E", f"同类错误重复，快速失败", fingerprint=error_dedup.get_fingerprint(e))
                break
            tool_name_from_error = step.get("skill", "")
            if tool_name_from_error and tool_name_from_error not in failed_tools:
                failed_tools.append(tool_name_from_error)
            logger.step_retry(step_num, attempt + 1, 2, str(e))
            if attempt < 2:
                await asyncio.sleep(1)

    # 重试耗尽 → 部分结果挽救：拼接已完成步骤的结果
    partial = ""
    try:
        successful = [s for s in plan_steps if s.get("status") == "success" and s.get("result")]
        if successful:
            partial = "\n\n".join([f"### Step {s['step']}: {s['purpose']}\n{s['result']}" for s in successful])
    except Exception:
        pass

    now = datetime.datetime.now().isoformat()
    new_steps = list(plan_steps)
    new_steps[idx] = PlanStep(
        step=step_num, skill=skill, purpose=purpose, type=step["type"],
        status="failed", result=partial or str(e), retry_count=2, executed_at=now,
    )
    logger.step_end(step_num, success=False, error=str(e))
    return {"plan_steps": new_steps, "_failed_tools": failed_tools}


def _build_context_for_step(step: dict, state: dict, ctx: AgentContext = None) -> list:
    """构建上下文消息：对话历史 + 已完成步骤摘要 + 用户约束 + 当前任务"""
    messages = []

    # 对话历史
    if ctx and ctx.memory and ctx.memory.enabled:
        history = ctx.memory.get_history()
        if history:
            messages.extend(history)

    # 已完成步骤摘要
    completed = [s for s in state.get("plan_steps", []) if s.get("status") == "success"]
    if completed:
        summary = "\n".join([f"- Step {s['step']} [{s['skill']}]: {s.get('result', '')}" for s in completed])
        messages.append(("system", f"已完成的步骤摘要：\n{summary}"))

    # 用户约束
    constraints = state.get("constraints", "")
    if constraints:
        messages.append(("system", f"[用户约束] {constraints}"))

    messages.append(("user", f"请执行以下信息收集任务：{step['purpose']}"))
    return messages


async def _run_react_step(step, state, ctx, llm, logger, failed_tools: list = None) -> tuple:
    """执行步骤（调用 ReAct 子图）。返回 (final_result, tool_results)"""
    from agents.common_react.graph import run_react_subgraph
    from agents.shared.tool_utils import merge_tools_for_step
    from tools.skills import SkillPromptBuilder

    registry = ctx.skill_registry
    tools = merge_tools_for_step(registry, step["skill"], selected_skills=state.get("selected_skills", []), logger=logger)

    # 构建上下文（原 build_context_for_step，内联）
    context_messages = _build_context_for_step(step, state, ctx)
    tools_detail = SkillPromptBuilder.build_tools_detail_prompt(registry, step["skill"])

    # 在 context 前插入工具详情
    full_context = [
        ("system", f"可用工具详情：\n{tools_detail}"),
        *context_messages,
    ]

    result = await run_react_subgraph(
        llm=llm,
        tools=tools,
        step_purpose=step["purpose"],
        context_messages=full_context,
        budget=ctx.budget,
        logger=logger,
        metadata={"pdor_context": "react_step"},
        failed_tools=failed_tools,
        template_id=state.get("template_id"),
    )

    return result["final_result"], result["tool_results"]



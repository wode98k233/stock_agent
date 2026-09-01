"""Executor 步骤函数 — 执行单个 plan step（编排层）

职责拆分：
- execute_step: 主入口，编排预算检查 → 重试循环 → 结果构建
- _build_step_prompt: 构建执行提示词
- _run_step_with_retry: 重试循环（含预算检查、指纹去重）
- _handle_step_failure: 用户交互（跳过/总结/自定义）
"""

import asyncio
import datetime
import re
import time
import traceback

from agents.common_react.graph import run_react_subgraph

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.prompts import EXECUTOR_SYSTEM
from agents.user_decision import ask_user_decision, format_step_status
from agents.plan.analyze import extract_step_info, is_abnormal_result
from agents.shared.exception_utils import handle_budget_exceeded
from agents.shared.tool_utils import merge_tools_for_step
from agents.shared.step_utils import restore_exempt_window, check_budget_with_exempt, ErrorDeduplicator
from tools.skills import SkillPromptBuilder
from utils.budget import BudgetExceeded, BudgetExemptWindow
from utils.logger import RequestContext, ensure_radar

# 匹配 LLM 幻觉出的 tool_call XML（循环检测强制总结时常见）
_TOOL_CALL_XML_PATTERN = re.compile(r"<tool_call>\s*<function=")

MAX_USER_APPROVED_OVERRUN = 3


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

def _build_step_result(step_idx, skill_name, status, summary,
                       key_data=None, error_info=None, tool_unavailable=False):
    """构建结构化步骤结果"""
    return {
        "step_idx": step_idx,
        "skill": skill_name,
        "status": status,
        "summary": summary if summary else "",
        "key_data": key_data or {},
        "error_info": error_info,
        "tool_unavailable": tool_unavailable,
        "timestamp": datetime.datetime.now().isoformat(),
    }


def _check_budget_with_exempt(state, budget, logger, exempt_window):
    """统一的预算检查 — 委托共享版本（overrun_limit=MAX_USER_APPROVED_OVERRUN）"""
    return check_budget_with_exempt(state, budget, logger, exempt_window, overrun_limit=MAX_USER_APPROVED_OVERRUN)

def _handle_step_failure(step_info, state, last_error, memory, logger, progress):
    """步骤失败后与用户交互，返回 return_dict 或 None（表示重试）"""
    step_num = step_info["step_num"]
    skill_name = step_info["skill_name"]
    step_idx = step_info["step_idx"]
    purpose = step_info["purpose"]
    plan = state["plan"]

    past = state.get("past_steps", [])
    status_lines, missing_lines = format_step_status(plan, past, step_idx)
    decision = ask_user_decision(
        header=f"Step {step_num} 执行失败：{last_error[:100]}",
        status_lines=status_lines + [""] + missing_lines,
        options=["跳过此步，继续执行后续步骤", "基于已有数据生成总结", "告诉我你想怎么处理"],
    )

    if "跳过" in decision:
        error_result = f"Step {step_num} 执行失败（用户选择跳过）：{last_error}"
        memory.add_ai(f"Step {step_num} [{skill_name}]: {error_result}")
        logger.step_end(step_num, success=False, error=error_result[:200])
        if progress:
            progress.warning(f"Step {step_num} 跳过")
        skip_result = _build_step_result(step_idx, skill_name, "fail", error_result, error_info=last_error[:200])
        skip_result["observer_decision"] = "pass"
        skip_result["observer_reasoning"] = "用户选择跳过"
        return {
            "past_steps": [(f"Step {step_num}: {purpose}", error_result)],
            "current_step": step_idx + 1,
            "step_results": [skip_result],
            "observer_log": [{"step_idx": step_idx, "decision": "pass", "reasoning": "用户选择跳过"}],
        }

    if "总结" in decision:
        summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
        return {"response": f"用户选择基于已有数据生成总结:\n\n{summary}"}

    # 用户自定义处理
    error_result = f"Step {step_num} 执行失败：{last_error}"
    memory.add_ai(f"Step {step_num} [{skill_name}]: {error_result}")
    custom_result = _build_step_result(step_idx, skill_name, "fail", error_result, error_info=last_error[:200])
    custom_result["observer_decision"] = "pass"
    custom_result["observer_reasoning"] = "用户自定义处理"
    return {
        "past_steps": [(f"Step {step_num}: {purpose}", error_result)],
        "current_step": step_idx + 1,
        "step_results": [custom_result],
        "observer_log": [{"step_idx": step_idx, "decision": "pass", "reasoning": "用户自定义处理"}],
    }


# ══════════════════════════════════════════════════════════════
# 核心执行逻辑
# ══════════════════════════════════════════════════════════════

async def _run_step_with_retry(state, step_info, tools, registry, ctx, budget, exempt_window, logger, progress):
    """执行单步，含重试循环。返回 (return_dict, exempt_window)。"""
    step_num = step_info["step_num"]
    skill_name = step_info["skill_name"]
    step_idx = step_info["step_idx"]
    purpose = step_info["purpose"]

    from utils.llm_factory import get_llm
    llm = get_llm()

    max_retries = Config.PLAN_EXECUTOR_MAX_RETRIES
    retry_count = 0
    error_dedup = ErrorDeduplicator(strategy="set")

    while retry_count < max_retries:
        # 预算检查
        ok, exempt_window, budget_updates = _check_budget_with_exempt(state, budget, logger, exempt_window)
        if not ok:
            if budget_updates.get("_budget_exceeded"):
                break
            return budget_updates, exempt_window

        # 构建上下文并执行子图
        context_msgs = []

        # 注入对话历史
        if ctx.memory and ctx.memory.enabled:
            history = ctx.memory.get_history()
            if history:
                context_msgs.extend(history)

        context_msgs.append(("user", step_info["instruction"]))
        user_constraints = state.get("user_constraints", "")
        if user_constraints:
            context_msgs.append(("system", f"[用户约束] {user_constraints}"))

        try:
            result = await run_react_subgraph(
                llm=llm,
                tools=list(tools),
                step_purpose=purpose,
                context_messages=context_msgs,
                max_iterations=Config.PLAN_EXECUTOR_TOOL_CALLS,
                budget=budget,
                logger=logger,
                metadata={"plan_context": "executor_step"},
                failed_tools=[],
                template_id=state.get("template_id"),
            )

            result_text = result["final_result"]

            # 收集 tool_calls
            collected_tool_calls = []
            for tr in result.get("tool_results", []):
                collected_tool_calls.append({
                    "tool_name": tr.get("tool", "unknown"),
                    "tool_input": str(tr.get("input", "")),
                    "tool_output": tr.get("output", ""),
                    "tool_call_id": "",
                })

            # 异常结果检查
            if is_abnormal_result(result_text) or result_text.strip().startswith("Sorry, need more steps"):
                raise ValueError(f"Agent 返回异常结果: {repr(result_text)}")

            # tool_call XML 幻觉检查：循环检测强制总结时 LLM 可能输出 tool_call XML
            if result_text and _TOOL_CALL_XML_PATTERN.search(result_text):
                logger.warning("E", f"Step {step_num} 结果包含 tool_call XML 幻觉，视为异常")
                raise ValueError(f"Agent 返回 tool_call XML 幻觉: {repr(result_text[:200])}")

            logger.info("E", f"[OK] Step {step_num} 执行完成")

            ctx.memory.add_ai(f"Step {step_num} [{skill_name}]: {result_text}")

            new_key_data = {
                **state.get("key_data", {}),
                f"step_{step_idx}": {"purpose": purpose, "summary": result_text},
            }

            if progress:
                progress.step_complete(step_num, result_text[:100])
            logger.step_end(step_num, success=True, summary=result_text)

            step_result = _build_step_result(step_idx, skill_name, "success", result_text,
                                              key_data=new_key_data.get(f"step_{step_idx}", {}))
            step_result["observer_decision"] = "pass"
            step_result["observer_reasoning"] = ""

            return_dict = {
                "past_steps": [(f"Step {step_num}: {purpose}", result_text)],
                "current_step": step_idx + 1,
                "key_data": new_key_data,
                "step_results": [step_result],
                "observer_log": [{"step_idx": step_idx, "decision": "pass", "reasoning": ""}],
            }
            if collected_tool_calls:
                return_dict["tool_calls"] = collected_tool_calls
            return return_dict, exempt_window

        except BudgetExceeded:
            decision = handle_budget_exceeded(state, logger)
            if decision.get("_user_approved_overrun"):
                exempt_window = BudgetExemptWindow(
                    calls_limit=Config.BUDGET_EXEMPT_CALLS_LIMIT,
                    time_limit=Config.BUDGET_EXEMPT_TIME_LIMIT,
                )
                state["budget_exempt"] = exempt_window.to_dict()
                continue
            break

        except Exception as e:
            retry_count += 1
            last_error = str(e) + str(traceback.format_exc())

            if error_dedup.is_duplicate(e):
                error_fp = error_dedup.get_fingerprint(e)
                logger.error("E", f"│ 同类错误重复，快速失败", fingerprint=error_fp)
                error_result = f"Step {step_num} 执行失败：{last_error}"
                ctx.memory.add_ai(f"Step {step_num} [{skill_name}]: {error_result}")
                logger.step_end(step_num, success=False, error=error_result[:200])
                if progress:
                    progress.error(f"Step {step_num} 快速失败: {error_fp}")
                fail_result = _build_step_result(step_idx, skill_name, "fail", error_result,
                                                  error_info=last_error[:200], tool_unavailable=True)
                fail_result["observer_decision"] = "replan"
                fail_result["observer_reasoning"] = f"同类错误重复 ({error_fp})"
                return {
                    "past_steps": [(f"Step {step_num}: {purpose}", error_result)],
                    "current_step": step_idx + 1,
                    "step_results": [fail_result],
                    "observer_log": [{"step_idx": step_idx, "decision": "replan", "reasoning": f"同类错误重复 ({error_fp})"}],
                }, exempt_window

            logger.step_retry(step_num, retry_count, max_retries, last_error)

            ctx_rc = RequestContext.current()
            if ctx_rc:
                ctx_rc.record_retry()
            if progress:
                progress.warning(f"Step {step_num} 重试 {retry_count}/{max_retries}")

            if retry_count < max_retries:
                await asyncio.sleep(1)
            else:
                logger.step_end(step_num, success=False, error=last_error)
                return _handle_step_failure(step_info, state, last_error, ctx.memory, logger, progress), exempt_window

    # 重试耗尽 / 预算超限
    past = state.get("past_steps", [])
    if past:
        summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
        return {"response": f"预算超限，已基于已有数据生成总结:\n\n{summary}"}, exempt_window
    return {"response": "预算超限，无可用结果"}, exempt_window


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

async def execute_step(state: PlanExecute, ctx: AgentContext, run_config=None):
    """执行当前 plan step"""
    plan = state["plan"]
    step_idx = state["current_step"]

    if step_idx >= len(plan):
        return {"response": "所有步骤已执行完毕"}

    step_info = extract_step_info(plan, step_idx)
    step_info["step_idx"] = step_idx
    logger = ensure_radar(ctx.logger)
    progress = ctx.progress_reporter
    budget = ctx.budget

    # 恢复豁免窗口
    exempt_window = restore_exempt_window(state)

    # 预算检查
    ok, exempt_window, updates = _check_budget_with_exempt(state, budget, logger, exempt_window)
    if not ok:
        if updates.get("_budget_exceeded"):
            past = updates.get("past_steps", state.get("past_steps", []))
            if past:
                summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
                return {"response": f"预算超限，已基于已有数据生成总结:\n\n{summary}"}
            return {"response": "预算超限，无可用结果"}
        return updates

    logger.phase("执行步骤")
    logger.step_start(step_info["step_num"], step_info["skill_name"], step_info["purpose"], step_info["instruction"])
    if progress:
        progress.step_start(step_info["step_num"], f"[{step_info['skill_name']}] {step_info['purpose']}")

    # 用户确认
    from agents.utils import _confirm_step_execution
    if not _confirm_step_execution(step_info["step_num"], step_info["skill_name"], step_info["purpose"], step_info["instruction"], logger):
        past = state.get("past_steps", [])
        if past:
            summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
            return {"response": f"用户取消了执行，已完成的结果:\n\n{summary}"}
        return {"response": "用户取消了执行"}

    # 加载工具
    tools = merge_tools_for_step(ctx.skill_registry, step_info["skill_name"], state.get("selected_skills", []), logger, log_tag="E")
    logger.debug("E", f"加载工具: {[t.name for t in tools]}")

    # 执行
    return_dict, exempt_window = await _run_step_with_retry(
        state, step_info, tools, ctx.skill_registry, ctx, budget, exempt_window, logger, progress,
    )
    return return_dict

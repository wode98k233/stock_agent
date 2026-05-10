"""Executor 步骤函数 — 执行单个 plan step（编排层）

保留原 execute_step 的编排逻辑，分析逻辑委托给 analyze.py，
异常处理委托给 handler.py 和 executor_callbacks.py。
"""

import time
import traceback

from langchain.agents import create_agent as create_react_agent

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.executor_callbacks import (
    ExecutionState,
    ExecutionStateCallback,
    generate_partial_summary,
)
from agents.prompts import EXECUTOR_SYSTEM
from agents.user_decision import ask_user_decision, format_step_status
from agents.plan.analyze import extract_step_info, has_useful_results
from agents.plan.handler import handle_budget_exceeded
from tools.skills import SkillPromptBuilder
from utils.budget import BudgetExceeded
from utils.llm_factory import TokenTracker, LLMDebugCallback
from utils.logger import RequestContext, ensure_radar

# === 预算豁免窗口 ===
MAX_USER_APPROVED_OVERRUN = 3  # 用户选择"继续执行"的最大总次数

import datetime


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


class BudgetExemptWindow:
    """预算豁免窗口 — 用户确认超限后，短期内跳过 budget check"""

    def __init__(self, calls_limit=3, time_limit=60):
        self.remaining_calls = calls_limit
        self.start_time = time.time()
        self.time_limit = time_limit

    def is_active(self) -> bool:
        if self.remaining_calls <= 0:
            return False
        if time.time() - self.start_time > self.time_limit:
            return False
        return True

    def consume(self):
        self.remaining_calls -= 1

    def to_dict(self) -> dict:
        return {
            "remaining_calls": self.remaining_calls,
            "start_time": self.start_time,
            "time_limit": self.time_limit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetExemptWindow":
        w = cls(calls_limit=d["remaining_calls"], time_limit=d["time_limit"])
        w.start_time = d["start_time"]
        return w


def _check_budget_with_exempt(state, budget, logger, exempt_window):
    """统一的预算检查 — 考虑豁免窗口

    Returns:
        (ok, exempt_window, updates): ok=True 表示可以继续，False 表示需要返回
    """
    # 有活跃的豁免窗口，跳过检查
    if exempt_window and exempt_window.is_active():
        exempt_window.consume()
        logger.info("B", f"豁免窗口内，跳过 budget check（剩余 {exempt_window.remaining_calls} 次）")
        return True, exempt_window, {}

    # 检查预算
    try:
        budget.check(logger)
        return True, exempt_window, {}
    except BudgetExceeded:
        pass

    # 检查总次数限制
    overrun_count = state.get("user_approved_overrun_count") or 0
    if overrun_count >= MAX_USER_APPROVED_OVERRUN:
        logger.warning("B", f"用户已连续选择继续执行 {overrun_count} 次，强制终止")
        past = state.get("past_steps", [])
        summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
        return False, exempt_window, {"response": f"预算持续超限，已基于已有数据生成总结:\n\n{summary}"}

    # 让用户决策
    result = handle_budget_exceeded(state, logger)

    if result.get("_user_approved_overrun"):
        new_window = BudgetExemptWindow(calls_limit=3, time_limit=60)
        new_count = overrun_count + 1
        logger.info("B", f"用户选择继续执行（第 {new_count} 次），创建豁免窗口")
        return True, new_window, {
            "budget_exempt": new_window.to_dict(),
            "user_approved_overrun_count": new_count,
        }

    # 用户选择终止
    return False, exempt_window, result


async def execute_step(state: PlanExecute, ctx: AgentContext):
    """执行当前 plan step"""
    plan = state["plan"]
    step_idx = state["current_step"]

    if step_idx >= len(plan):
        return {"response": "所有步骤已执行完毕"}

    step_info = extract_step_info(plan, step_idx)
    skill_name = step_info["skill_name"]
    instruction = step_info["instruction"]
    step_num = step_info["step_num"]
    purpose = step_info["purpose"]

    logger = ensure_radar(ctx.logger)
    memory = ctx.memory
    registry = ctx.skill_registry
    progress = ctx.progress_reporter

    from utils.llm_factory import get_llm
    llm = get_llm()

    budget = ctx.budget

    # 恢复豁免窗口（重规划后保持）
    exempt_window = None
    exempt_data = state.get("budget_exempt")
    if exempt_data:
        try:
            exempt_window = BudgetExemptWindow.from_dict(exempt_data)
            if not exempt_window.is_active():
                exempt_window = None
        except Exception:
            exempt_window = None

    # 统一的预算检查
    ok, exempt_window, updates = _check_budget_with_exempt(state, budget, logger, exempt_window)
    if not ok:
        return updates

    logger.phase("执行步骤")
    logger.step_start(step_num, skill_name, purpose, instruction)

    if progress:
        progress.step_start(step_num, f"[{skill_name}] {purpose}")

    from agents.utils import _confirm_step_execution
    if not _confirm_step_execution(step_num, skill_name, purpose, instruction, logger):
        past = state.get("past_steps", [])
        if past:
            summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
            return {"response": f"用户取消了执行，已完成的结果:\n\n{summary}"}
        return {"response": "用户取消了执行"}

    tools = registry.get_tools(skill_name)
    if not tools:
        tools = registry.get_all_tools()
        logger.warn("E", f"技能 '{skill_name}' 无对应工具，加载全部")

    logger.debug("E", f"加载工具: {[t.name for t in tools]}")

    tool_desc = "\n".join(f"- {t.name}: {t.description}" for t in tools)
    plan_str = "\n".join(
        f"  {s.get('step', i+1)}. [{s.get('skill','')}] {s.get('purpose','')}"
        for i, s in enumerate(plan)
    )
    tools_detail = SkillPromptBuilder.build_tools_detail_prompt(registry, skill_name)

    user_constraints = state.get("user_constraints", "")
    constraints_block = f"\n\n[用户约束] {user_constraints}" if user_constraints else ""

    exec_messages = [
        ("system", f"""{EXECUTOR_SYSTEM}

当前执行计划:
{plan_str}

你正在执行第 {step_num} 步。
{constraints_block}
可用工具:
{tool_desc}

工具详情介绍:
{tools_detail}
"""),
        ("user", f"执行指令：{instruction}"),
    ]

    max_retries = Config.PLAN_EXECUTOR_MAX_RETRIES
    retry_count = 0
    last_error = None
    error_fingerprints = set()

    while retry_count < max_retries:
        try:
            # 统一的预算检查（合并原有两处检查）
            ok, exempt_window, budget_updates = _check_budget_with_exempt(
                state, budget, logger, exempt_window
            )
            if not ok:
                return budget_updates

            agent = create_react_agent(llm, tools)
            exec_state = ExecutionState()

            callbacks = [
                TokenTracker(logger, f"executor-step{step_num}", budget=budget),
                ExecutionStateCallback(exec_state, logger),
            ]

            if Config.LOG_LEVEL == "DEBUG":
                callbacks.append(LLMDebugCallback(logger, f"executor-step{step_num}"))

            recorder = ctx.trace_recorder
            if recorder:
                callbacks.append(recorder)

            agent_resp = await agent.ainvoke(
                {"messages": exec_messages},
                config={
                    "recursion_limit": Config.PLAN_EXECUTOR_TOOL_CALLS,
                    "callbacks": callbacks,
                },
            )

            result_text = agent_resp["messages"][-1].content

            from agents.plan.analyze import is_abnormal_result
            if is_abnormal_result(result_text) or result_text.strip().startswith("Sorry, need more steps"):
                if exec_state.has_useful_results():
                    logger.warning("E", f"Agent 达到限制，但已有部分结果，生成总结")
                    result_text = await generate_partial_summary(
                        exec_state, purpose, instruction, llm, logger
                    )
                    result_text = "⚠️ 部分完成（达到迭代限制）\n\n" + result_text
                else:
                    raise ValueError(f"Agent 返回异常结果: {repr(result_text)}")

            logger.info("E", f"[OK] Step {step_num} 执行完成")
            logger.debug("E", f"结果：{result_text[:200]}...")

            from agents.utils import _generate_step_summary
            step_summary = await _generate_step_summary(result_text, purpose, llm, logger)

            memory.add_ai(f"Step {step_num} [{skill_name}]: {result_text}")

            new_key_data = {
                **state.get("key_data", {}),
                f"step_{step_idx}": {
                    "purpose": purpose,
                    "summary": result_text,
                    "step_data": step_summary,
                },
            }

            ctx_rc = RequestContext.current()
            if ctx_rc:
                ctx_rc.record_step(success=True)

            if progress:
                progress.step_complete(step_num, result_text[:100])

            logger.step_end(step_num, success=True, summary=result_text)

            # 构建步骤结果（成功步骤不调 Observer，节省 token）
            step_result = _build_step_result(
                step_idx, skill_name, "success", result_text,
                key_data=new_key_data.get(f"step_{step_idx}", {}),
            )
            step_result["observer_decision"] = "pass"
            step_result["observer_reasoning"] = ""

            obs_log_entry = {
                "step_idx": step_idx,
                "decision": "pass",
                "reasoning": "",
            }

            return {
                "past_steps": [(f"Step {step_num}: {purpose}", result_text)],
                "current_step": step_idx + 1,
                "key_data": new_key_data,
                "step_results": [step_result],
                "observer_log": [obs_log_entry],
            }

        except Exception as e:
            retry_count += 1
            last_error = str(e) + str(traceback.format_exc())

            if "exec_state" in locals() and exec_state.has_useful_results():
                is_budget = isinstance(e, BudgetExceeded)
                log_msg = "达到预算，但已有部分结果，尝试生成总结" if is_budget else "执行异常，但已有部分结果，尝试生成总结"
                prefix = "⚠️ 部分完成（预算超限）\n\n" if is_budget else "⚠️ 部分完成（执行异常）\n\n"

                logger.warning("E", log_msg)
                try:
                    result_text = await generate_partial_summary(
                        exec_state, purpose, instruction, llm, logger
                    )
                    result_text = prefix + result_text
                    memory.add_ai(f"Step {step_num} [{skill_name}]: {result_text}")
                    logger.step_end(step_num, success=False, summary=result_text)

                    # 部分完成：直接标记，不调 Observer（节省 token）
                    step_result = _build_step_result(
                        step_idx, skill_name, "partial", result_text,
                        error_info=last_error[:200] if last_error else None,
                    )
                    step_result["observer_decision"] = "pass"
                    step_result["observer_reasoning"] = ""

                    return {
                        "past_steps": [(f"Step {step_num}: {purpose} (部分完成)", result_text)],
                        "current_step": step_idx + 1,
                        "step_results": [step_result],
                        "observer_log": [{
                            "step_idx": step_idx,
                            "decision": "pass",
                            "reasoning": "",
                        }],
                    }
                except Exception:
                    logger.warning("E", f"生成部分总结也失败")

            from agents.utils import _error_fingerprint
            error_fp = _error_fingerprint(e)
            if error_fp in error_fingerprints:
                logger.error("E", f"│ 同类错误重复，快速失败", fingerprint=error_fp)
                error_result = f"Step {step_num} 执行失败：{last_error}"
                memory.add_ai(f"Step {step_num} [{skill_name}]: {error_result}")

                ctx_rc = RequestContext.current()
                if ctx_rc:
                    ctx_rc.record_step(success=False)
                if progress:
                    progress.error(f"Step {step_num} 快速失败: {error_fp}")

                # 同类错误重复：直接标记失败，不调 Observer
                fail_result = _build_step_result(
                    step_idx, skill_name, "fail", error_result,
                    error_info=last_error[:200],
                    tool_unavailable=True,
                )
                fail_result["observer_decision"] = "replan"
                fail_result["observer_reasoning"] = f"同类错误重复 ({error_fp})"

                return {
                    "past_steps": [(f"Step {step_num}: {purpose}", error_result)],
                    "current_step": step_idx + 1,
                    "step_results": [fail_result],
                    "observer_log": [{
                        "step_idx": step_idx,
                        "decision": "replan",
                        "reasoning": f"同类错误重复 ({error_fp})",
                    }],
                }
            error_fingerprints.add(error_fp)

            logger.step_retry(step_num, retry_count, max_retries, last_error)

            ctx_rc = RequestContext.current()
            if ctx_rc:
                ctx_rc.record_retry()
            if progress:
                progress.warning(f"Step {step_num} 重试 {retry_count}/{max_retries}")

            if retry_count < max_retries:
                import asyncio
                await asyncio.sleep(1)
            else:
                logger.step_end(step_num, success=False, error=last_error)

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
                    ctx_rc = RequestContext.current()
                    if ctx_rc:
                        ctx_rc.record_step(success=False)
                    if progress:
                        progress.warning(f"Step {step_num} 跳过")
                    skip_result = _build_step_result(
                        step_idx, skill_name, "fail", error_result,
                        error_info=last_error[:200],
                    )
                    skip_result["observer_decision"] = "pass"
                    skip_result["observer_reasoning"] = "用户选择跳过"
                    return {
                        "past_steps": [(f"Step {step_num}: {purpose}", error_result)],
                        "current_step": step_idx + 1,
                        "step_results": [skip_result],
                        "observer_log": [{
                            "step_idx": step_idx,
                            "decision": "pass",
                            "reasoning": "用户选择跳过",
                        }],
                    }
                elif "总结" in decision:
                    summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
                    return {"response": f"用户选择基于已有数据生成总结:\n\n{summary}"}
                else:
                    error_result = f"Step {step_num} 执行失败：{last_error}"
                    memory.add_ai(f"Step {step_num} [{skill_name}]: {error_result}")
                    custom_result = _build_step_result(
                        step_idx, skill_name, "fail", error_result,
                        error_info=last_error[:200],
                    )
                    custom_result["observer_decision"] = "pass"
                    custom_result["observer_reasoning"] = "用户自定义处理"
                    return {
                        "past_steps": [(f"Step {step_num}: {purpose}", error_result)],
                        "current_step": step_idx + 1,
                        "step_results": [custom_result],
                        "observer_log": [{
                            "step_idx": step_idx,
                            "decision": obs_result.decision,
                            "reasoning": obs_result.reasoning,
                        }],
                    }

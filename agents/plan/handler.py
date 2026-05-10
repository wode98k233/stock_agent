"""Plan Agent 异常处理编排 — 调用 LLM、读写 state、用户交互"""
import traceback
from agents.plan.shared import generate_summary


def handle_budget_exceeded(state, logger):
    """Budget 超限处理 — 用户确认后跳过/终止"""
    from agents.user_decision import ask_user_decision, format_step_status

    past = state.get("past_steps", [])
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)

    logger.warning("B", "预算超限")
    status_lines, missing_lines = format_step_status(plan, past, current_step)
    decision = ask_user_decision(
        header=f"预算即将超限，已完成 {len(past)} 步",
        status_lines=status_lines + [""] + missing_lines,
        options=["基于已有数据生成总结", "继续执行（可能超限）"],
    )

    if "继续" in decision:
        logger.info("B", "用户选择继续执行")
        return {"_continue": True, "_user_approved_overrun": True}

    summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
    return {"response": f"预算超限，已完成的结果:\n\n{summary}"}


def handle_execution_error(e, user_input, last_state, logger):
    """通用异常处理 — 有部分结果则总结，否则报错"""
    from utils.logger import ensure_radar

    logger = ensure_radar(logger)
    logger.error("R", "执行异常", error=str(e) + traceback.format_exc())

    past_steps = last_state.get("past_steps", [])
    key_data = last_state.get("key_data", {})
    user_constraints = last_state.get("user_constraints", "")

    if not past_steps:
        return "分析过程遇到问题，没有可用的结果。"

    try:
        final_response = generate_summary(user_input, user_constraints, past_steps, key_data, logger)
        logger.info("R", "成功从异常中恢复并生成总结")
        return final_response
    except Exception as summary_e:
        logger.error("R", f"生成异常总结失败: {summary_e}")
        return "分析过程遇到问题，但已有部分结果:\n\n" + \
               "\n".join([f"- {desc}: {res}" for desc, res in past_steps])

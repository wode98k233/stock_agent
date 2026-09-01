"""共享异常处理函数 — Plan/PDOR/Unified 共用

原 agents/plan/handler.py 迁移至此。
"""
import traceback
from agents.shared.stream_utils import generate_summary


def handle_budget_exceeded(state, logger):
    """Budget 超限处理 — 用户确认后跳过/终止

    支持 Web 模式（通过 ContextVar 回调）和 CLI 模式（交互式 input）。
    """
    from agents.shared.budget_ctx import budget_decision_ctx
    from agents.user_decision import ask_user_decision, format_step_status

    past = state.get("past_steps", [])
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)

    logger.warning("B", "预算超限")

    # Web 模式：通过 SSE 回调让用户决策
    web_callback = budget_decision_ctx.get()
    if web_callback is not None:
        status_lines, missing_lines = format_step_status(plan, past, current_step)
        decision = web_callback({
            "reason": "budget_exceeded",
            "completed_steps": len(past),
            "status_lines": status_lines,
            "missing_lines": missing_lines,
        })
        if decision == "continue":
            logger.info("B", "Web 用户选择继续执行")
            return {"_continue": True, "_user_approved_overrun": True}
        summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
        return {"response": f"预算超限，已完成的结果:\n\n{summary}"}

    # CLI 模式：交互式 input
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

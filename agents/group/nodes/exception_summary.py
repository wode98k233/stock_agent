"""异常总结节点 — 处理空计划等异常情况，直接告知用户原因"""
from agents.group.state import GroupState
from agents.agent_context import AgentContext


async def exception_summary_node(state: GroupState, ctx: AgentContext, run_config=None) -> dict:
    """异常总结 — 将规划失败原因直接作为 response 返回给用户"""
    reason = state.get("observation", "")
    error_msg = state.get("_error_message", "")

    if error_msg:
        response = f"调度失败：{error_msg}"
    elif reason == "error":
        response = "调度失败：无法生成有效的执行计划，请尝试更具体的问题描述"
    else:
        response = f"调度异常：{reason}" if reason else "调度异常：未知错误"

    ctx.logger.warning("ES", response)
    return {"response": response}

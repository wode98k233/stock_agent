"""分类节点 — 复用 classify_input，过滤非股票问题"""
from agents.group.state import GroupState
from agents.agent_context import AgentContext
from agents.common import classify_input


async def classifier_node(state: GroupState, ctx: AgentContext, run_config=None) -> dict:
    """分类节点 — 判断是否为股票相关问题，非股票直接返回 response"""
    llm = ctx.budget._llm if hasattr(ctx.budget, '_llm') else None
    if not llm:
        from utils.llm_factory import get_llm
        llm = get_llm()

    result = await classify_input(
        state["input"],
        ctx.memory,
        llm,
        ctx.logger,
        ctx.budget,
        metadata={"agent_mode": "agent_group"},
        run_config=run_config,
    )

    # 只回传 GroupState 已声明的字段（title 的落库/推送已由 classify_input 内部回调完成）
    if result.get("response"):
        return {"response": result["response"]}
    if result.get("clarified_input"):
        return {"input": result["clarified_input"]}
    return {}

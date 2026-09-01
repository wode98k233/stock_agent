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

    return result

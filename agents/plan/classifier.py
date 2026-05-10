"""Classifier 步骤函数 — 输入分类路由"""

from agents.state import PlanExecute
from agents.agent_context import AgentContext
from utils.logger import ensure_radar
from utils.llm_factory import get_llm


async def classify_step(state: PlanExecute, ctx: AgentContext):
    """分类用户输入：直接回答 or 需要多步规划"""
    logger = ensure_radar(ctx.logger)
    progress = ctx.progress_reporter

    logger.phase("问题分类")
    if progress:
        progress.step_start(0, "问题分类")

    llm = get_llm()
    from agents.common import classify_input
    result = await classify_input(state["input"], ctx.memory, llm, logger)

    if result.get("response"):
        if progress:
            progress.classifier_result(False)
        return {"response": result["response"]}

    logger.info("C", "属于股票/金融相关，继续规划")
    if progress:
        progress.classifier_result(True)
    return {"plan": [], "current_step": 0, "progress_reporter": progress}


def classifier_should_end(state: PlanExecute):
    from langgraph.graph import END
    if state.get("response"):
        return END
    return "planner"

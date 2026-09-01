"""Classifier 步骤函数 — 输入分类路由（委托 shared ClassifyNode）"""

from agents.state import PlanExecute
from agents.agent_context import AgentContext
from utils.logger import ensure_radar


async def classify_step(state: PlanExecute, ctx: AgentContext, run_config=None):
    """分类用户输入：直接回答 or 需要多步规划"""
    from agents.shared.classify_node import make_classify_from_ctx

    logger = ensure_radar(ctx.logger)
    progress = ctx.progress_reporter

    logger.phase("问题分类")
    if progress:
        progress.step_start(0, "问题分类")

    classifier = make_classify_from_ctx(ctx)
    result = await classifier(state["input"], run_config=run_config)

    if not result["is_stock_related"]:
        if progress:
            progress.classifier_result(False)
        return {"response": result["response"]}

    logger.info("C", "属于股票/金融相关，继续规划")
    if progress:
        progress.classifier_result(True)
    return {"plan": [], "current_step": 0}


def classifier_should_end(state: PlanExecute):
    from langgraph.graph import END
    if state.get("response"):
        return END
    return "planner"

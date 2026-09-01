"""PDOR 图构建"""
from typing import Optional
from langgraph.graph import StateGraph, END

from agents.pdor.state import PdorState
from agents.pdor.node import (
    classifier_node,
    planner_node,
    executor_node,
    observer_node,
    adjuster_node,
    report_enhance_node,
)
from agents.analysis.dashboard_node import dashboard_node


def _after_classifier(state: PdorState) -> str:
    if state.get("response"):
        return END
    return "planner"


def _after_observer(state: PdorState) -> str:
    obs = state.get("observation", "")

    if obs == "need_replan":
        return "planner"
    if obs == "early_stop":
        return "report_enhance"
    if state.get("current_step_index", 0) >= len(state.get("plan_steps", [])):
        return "report_enhance"
    if obs == "need_adjust":
        return "adjuster"
    return "executor"


def _build_pdor_graph(
    logger=None,
    memory_mgr=None,
    skill_registry=None,
    progress_callback=None,
    budget=None,
    checkpointer=None,
):
    from agents.agent_context import AgentContext
    from utils.budget import BudgetControllerFactory
    from utils.progress import ProgressReporter

    ctx = AgentContext(
        logger=logger,
        memory=memory_mgr,
        skill_registry=skill_registry,
    )
    ctx.budget = budget or BudgetControllerFactory.create()
    ctx.progress_reporter = ProgressReporter(progress_callback) if progress_callback else None

    from agents.shared.graph_utils import wrap_node

    def _wrap(fn):
        return wrap_node(fn, ctx)

    graph = StateGraph(PdorState)

    graph.add_node("classifier", _wrap(classifier_node))
    graph.add_node("planner", _wrap(planner_node))
    graph.add_node("executor", _wrap(executor_node))
    graph.add_node("observer", _wrap(observer_node))
    graph.add_node("adjuster", _wrap(adjuster_node))
    graph.add_node("report_enhance", _wrap(report_enhance_node))
    graph.add_node("dashboard", _wrap(dashboard_node))

    graph.set_entry_point("classifier")
    graph.add_conditional_edges("classifier", _after_classifier, {END: END, "planner": "planner"})
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "observer")
    graph.add_conditional_edges("observer", _after_observer, {
        "planner": "planner",
        "executor": "executor",
        "adjuster": "adjuster",
        "report_enhance": "report_enhance",
        END: END,
    })
    graph.add_edge("adjuster", "executor")
    graph.add_edge("report_enhance", "dashboard")
    graph.add_edge("dashboard", END)

    return graph.compile(checkpointer=checkpointer)

"""Plan Agent — LangGraph 图构建

支持两种模式：
- mode="plan": 多步规划求解（planner → executor → replanner 循环）
- mode="unified": 大上下文单步执行（planner → unified_executor）
"""

from langgraph.graph import END, StateGraph

from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.plan.node import (
    classify_step, classifier_should_end, plan_step,
    execute_step, replan_step, unified_execute_step, report_enhance_node,
)
from agents.analysis.dashboard_node import dashboard_node
from tools.skills import SkillPromptBuilder
from utils.budget import get_budget_controller
from utils.progress import ProgressReporter


def make_step(fn, ctx):
    """创建支持 async 的 step 包装器，透传 LangGraph RunnableConfig"""
    from agents.shared.graph_utils import wrap_node
    return wrap_node(fn, ctx)


def replanner_should_end(state: PlanExecute):
    if state.get("response"):
        return END
    return "executor"


def create_plan_graph(
    mode: str,
    logger, memory_mgr, skill_registry,
    progress_callback=None,
    budget_controller=None,
    checkpointer=None,
):
    """创建 Plan Agent 的 StateGraph。

    Args:
        mode: "plan"（多步循环）或 "unified"（单步执行）
    """
    progress_reporter = ProgressReporter(progress_callback) if progress_callback else None
    ctx = AgentContext(
        logger=logger,
        memory=memory_mgr,
        skill_registry=skill_registry,
        budget=budget_controller or get_budget_controller(),
        progress_reporter=progress_reporter,
    )

    workflow = StateGraph(PlanExecute)

    # 公共节点
    workflow.add_node("classifier", make_step(classify_step, ctx))
    workflow.add_node("planner", make_step(plan_step, ctx))
    workflow.add_node("report_enhance", make_step(report_enhance_node, ctx))
    workflow.add_node("dashboard", make_step(dashboard_node, ctx))

    workflow.set_entry_point("classifier")

    workflow.add_conditional_edges(
        "classifier",
        classifier_should_end,
        {"planner": "planner", END: END},
    )
    workflow.add_edge("planner", "unified_executor" if mode == "unified" else "executor")

    # 模式差异节点
    if mode == "unified":
        workflow.add_node("unified_executor", make_step(unified_execute_step, ctx))
        workflow.add_edge("unified_executor", "report_enhance")
    else:
        workflow.add_node("executor", make_step(execute_step, ctx))
        workflow.add_node("replanner", make_step(replan_step, ctx))
        workflow.add_edge("executor", "replanner")
        workflow.add_conditional_edges(
            "replanner",
            replanner_should_end,
            {"executor": "executor", END: "report_enhance"},
        )

    workflow.add_edge("report_enhance", "dashboard")
    workflow.add_edge("dashboard", END)

    return workflow.compile(checkpointer=checkpointer)


# 向后兼容：保留旧函数名，内部委托给 create_plan_graph
def create_agent(logger, memory_mgr, skill_registry, progress_callback=None,
                 budget_controller=None, checkpointer=None):
    return create_plan_graph(
        mode="plan", logger=logger, memory_mgr=memory_mgr,
        skill_registry=skill_registry, progress_callback=progress_callback,
        budget_controller=budget_controller, checkpointer=checkpointer,
    )


def create_unified_agent(logger, memory_mgr, skill_registry, progress_callback=None,
                         budget_controller=None, checkpointer=None):
    return create_plan_graph(
        mode="unified", logger=logger, memory_mgr=memory_mgr,
        skill_registry=skill_registry, progress_callback=progress_callback,
        budget_controller=budget_controller, checkpointer=checkpointer,
    )

"""Plan Agent — LangGraph 图构建"""

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.plan.classifier import classify_step, classifier_should_end
from agents.plan.planner import plan_step
from agents.plan.executor import execute_step
from agents.plan.replanner import replan_step
from agents.plan.unified_executor import unified_execute_step
from tools.skills import SkillPromptBuilder
from utils.budget import get_budget_controller


def make_step(fn, ctx):
    """创建支持 async 的 step 包装器"""
    async def wrapper(state: PlanExecute):
        result = await fn(state, ctx)
        return result
    return wrapper


class ProgressReporter:
    """进度回调包装器，与 AgentContext 配合使用"""
    def __init__(self, callback):
        self._callback = callback

    def step_start(self, step_num, desc): ...
    def step_complete(self, step_num, result): ...
    def warning(self, msg): ...
    def error(self, msg): ...
    def milestone(self, msg): ...
    def classifier_result(self, needs_plan: bool): ...
    def planner_result(self, step_count: int): ...
    def replanner_result(self, new_count: int, purposes: list): ...
    def final(self): ...


def create_agent(logger, memory_mgr, skill_registry,
                 progress_callback=None,
                 budget_controller=None,
                 trace_recorder=None):
    """创建 PlanAndSolve Agent 的 StateGraph"""
    progress_reporter = ProgressReporter(progress_callback) if progress_callback else None
    ctx = AgentContext(
        logger=logger,
        memory=memory_mgr,
        skill_registry=skill_registry,
        budget=budget_controller or get_budget_controller(),
        progress_reporter=progress_reporter,
        trace_recorder=trace_recorder,
    )

    workflow = StateGraph(PlanExecute)

    workflow.add_node("classifier", make_step(classify_step, ctx))
    workflow.add_node("planner", make_step(plan_step, ctx))
    workflow.add_node("executor", make_step(execute_step, ctx))
    workflow.add_node("replanner", make_step(replan_step, ctx))

    workflow.set_entry_point("classifier")

    workflow.add_conditional_edges(
        "classifier",
        classifier_should_end,
        {"planner": "planner", END: END},
    )
    workflow.add_edge("planner", "executor")
    workflow.add_edge("executor", "replanner")
    workflow.add_conditional_edges(
        "replanner",
        replanner_should_end,
        {"executor": "executor", END: END},
    )

    return workflow.compile(checkpointer=MemorySaver())


def replanner_should_end(state: PlanExecute):
    if state.get("response"):
        return END
    return "executor"


def create_unified_agent(logger, memory_mgr, skill_registry,
                         progress_callback=None,
                         budget_controller=None,
                         trace_recorder=None):
    """创建 UnifiedPlan Agent 的 StateGraph（单步执行全部）"""
    progress_reporter = ProgressReporter(progress_callback) if progress_callback else None
    ctx = AgentContext(
        logger=logger,
        memory=memory_mgr,
        skill_registry=skill_registry,
        budget=budget_controller or get_budget_controller(),
        progress_reporter=progress_reporter,
        trace_recorder=trace_recorder,
    )

    workflow = StateGraph(PlanExecute)

    workflow.add_node("classifier", make_step(classify_step, ctx))
    workflow.add_node("unified_executor", make_step(unified_execute_step, ctx))

    workflow.set_entry_point("classifier")

    workflow.add_conditional_edges(
        "classifier",
        classifier_should_end,
        {"planner": "unified_executor", END: END},
    )
    workflow.add_edge("unified_executor", END)

    return workflow.compile(checkpointer=MemorySaver())

"""Plan/Unified Agent — 两种 Plan 模式共用入口

合并自原 agents.plan.agent 和 agents.plan.unified：
- PlanAndSolveAgent: 多步规划求解（planner → executor → replanner 循环）
- UnifiedPlanAgent: 大上下文单步执行（planner → unified_executor）

两者差异仅在 _build_graph 选择的图构造函数，其余由基类默认实现提供。
"""
from agents.plan.base import PlanBasedAgent
from agents.plan.graph import create_plan_graph


class PlanAndSolveAgent(PlanBasedAgent):
    """多步规划求解 Agent"""

    name = "plan_solve"
    display_name = "Plan & Solve"
    description = "先规划再执行，适合复杂分析。"

    def _build_graph(self, logger, memory, registry, progress_callback, budget, checkpointer):
        return create_plan_graph(
            mode="plan",
            logger=logger,
            memory_mgr=memory,
            skill_registry=registry,
            progress_callback=progress_callback,
            budget_controller=budget,
            checkpointer=checkpointer,
        )

    def _build_callbacks_tag(self) -> str:
        return "plan"


class UnifiedPlanAgent(PlanBasedAgent):
    """大上下文单步执行 Agent"""

    name = "unified_plan"
    display_name = "Unified Plan"
    description = "单次统一执行计划，适合结构清晰的问题。"

    def _build_graph(self, logger, memory, registry, progress_callback, budget, checkpointer):
        return create_plan_graph(
            mode="unified",
            logger=logger,
            memory_mgr=memory,
            skill_registry=registry,
            progress_callback=progress_callback,
            budget_controller=budget,
            checkpointer=checkpointer,
        )

    def _build_callbacks_tag(self) -> str:
        return "unified"

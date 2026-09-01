"""Plan Agent 包导出。

保持对外导出不变，但避免导入 agents.plan.shared 时急切加载 executor/graph。
"""

__all__ = ["PlanAndSolveAgent", "UnifiedPlanAgent", "create_agent", "create_unified_agent"]


def __getattr__(name):
    if name == "PlanAndSolveAgent":
        from agents.plan.plan_agents import PlanAndSolveAgent
        return PlanAndSolveAgent
    if name == "UnifiedPlanAgent":
        from agents.plan.plan_agents import UnifiedPlanAgent
        return UnifiedPlanAgent
    if name in {"create_agent", "create_unified_agent"}:
        from agents.plan.graph import create_agent, create_unified_agent
        return {"create_agent": create_agent, "create_unified_agent": create_unified_agent}[name]
    raise AttributeError(f"module 'agents.plan' has no attribute {name!r}")

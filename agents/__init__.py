"""
🤖 Agent 模块
自动发现并注册所有 Agent
"""
from agents.base import BaseAgent
from agents.factory import AgentFactory

# 导入所有 Agent 实现
from agents.react.agent import ReactStockAgent
from agents.plan.agent import PlanAndSolveAgent
from agents.plan.unified import UnifiedPlanAgent
from agents.scenario_agent import ScenarioAgent

# 🚀 自动注册所有 Agent
AgentFactory.register(PlanAndSolveAgent())
AgentFactory.register(ReactStockAgent())
AgentFactory.register(UnifiedPlanAgent())
AgentFactory.register(ScenarioAgent())


def register_all():
    """
    📋 注册所有 Agent（供外部调用）
    如需添加新 Agent，在此处导入并注册即可
    """
    pass


__all__ = ["BaseAgent", "AgentFactory", "register_all"]

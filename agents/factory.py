"""
🏭 Agent 工厂
负责 Agent 的注册、获取、列表
"""
from agents.base import BaseAgent


class AgentFactory:
    """
    🏭 Agent 工厂
    📋 管理所有已注册的 Agent
    """

    _agents = {}
    _current = "react_stock"

    @classmethod
    def register(cls, agent: BaseAgent):
        """
        📝 注册 Agent
        📥 agent: Agent 实例
        """
        cls._agents[agent.name] = agent

    @classmethod
    def get(cls, name=None) -> BaseAgent:
        """
        🎯 获取 Agent 实例
        📥 name: Agent 名称，默认返回当前
        📤 BaseAgent 实例
        """
        name = name or cls._current
        return cls._agents[name]

    @classmethod
    def list(cls) -> list:
        """
        📋 列出所有已注册的 Agent
        📤 list[str]: Agent 名称列表
        """
        return list(cls._agents.keys())

    @classmethod
    def set_default(cls, name: str):
        """
        ⚙️ 设置默认 Agent
        📥 name: Agent 名称
        """
        if name not in cls._agents:
            raise ValueError(f"Unknown agent: {name}")
        cls._current = name

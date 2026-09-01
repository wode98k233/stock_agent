import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agents.group.agent import GroupAgent
from config import Config


def test_group_agent_attributes():
    agent = GroupAgent()
    assert agent.name == "agent_group"
    assert "智能体" in agent.description


def test_group_agent_build_initial_state():
    agent = GroupAgent()
    state = agent._build_initial_state("分析茅台", "template_1", ["technical_analysis"])
    assert state["input"] == "分析茅台"
    assert state["template_id"] == "template_1"
    assert "technical_analysis" in state["selected_skills"]
    assert state["plan_steps"] == []


def test_group_agent_recursion_limit():
    agent = GroupAgent()
    limit = agent._get_recursion_limit()
    assert limit == Config.GROUP_MAX_STEPS * 8 + 20


def test_group_agent_callbacks_tag():
    agent = GroupAgent()
    assert agent._build_callbacks_tag() == "agent_group"

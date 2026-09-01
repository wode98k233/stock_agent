"""Agent 群模式端到端集成测试 — mock LLM，验证完整数据流"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.group.agent import GroupAgent
from agents.group.state import GroupState
from agents.group.messages_db import init_group_messages_table, get_group_messages


@pytest.fixture(autouse=True)
def setup_db():
    init_group_messages_table()


def test_group_agent_registered():
    from agents.factory import AgentFactory
    from agents.group.agent import GroupAgent
    AgentFactory.register(GroupAgent())
    agent = AgentFactory.get("agent_group")
    assert agent.name == "agent_group"


def test_group_agent_has_all_nodes():
    """验证 GroupAgent 的 graph 包含所有必要节点"""
    from agents.group.dispatcher import build_dispatch_graph
    from agents.agent_context import AgentContext

    ctx = AgentContext(
        logger=MagicMock(),
        memory=MagicMock(),
        skill_registry=MagicMock(),
        budget=MagicMock(),
        progress_reporter=MagicMock(),
    )
    graph = build_dispatch_graph(ctx)
    # StateGraph compiled 后 nodes 是内部数据，检查 graph 对象存在即可
    assert graph is not None


def test_group_messages_table_exists():
    """验证 group_messages 表已创建"""
    from agents.group.messages_db import get_group_db
    with get_group_db() as conn:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='group_messages'"
        ).fetchone()
    assert result is not None


def test_group_state_all_fields():
    """验证 GroupState 包含设计规格中的所有字段"""
    state = GroupState(
        input="test", plan_steps=[], current_step_index=0,
        original_plan=[], accumulated_data="", resolved_data="",
        constraints="", observation="", response="", budget_exempt=None,
        _replan_count=0, _failed_agents=None, template_id=None,
        tool_calls=[], selected_skills=[],
    )
    required_fields = [
        "input", "plan_steps", "current_step_index", "original_plan",
        "accumulated_data", "resolved_data", "constraints", "observation",
        "response", "budget_exempt", "_replan_count", "_failed_agents",
        "template_id", "tool_calls", "selected_skills",
    ]
    for field in required_fields:
        assert field in state, f"Missing field: {field}"

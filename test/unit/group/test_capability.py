import pytest
from agents.group.capability import get_capability, build_capabilities_prompt
from agents.group.subagents import AGENT_CLASSES, get_all_agent_info


def test_get_capability_exists():
    cap = get_capability("technical_analyst")
    assert cap["display_name"] == "K线技术分析师"
    assert "mx_data" in cap["assigned_skills"]


def test_get_capability_not_exists():
    with pytest.raises(KeyError, match="未知 agent"):
        get_capability("nonexistent_agent")


def test_all_capabilities_have_required_fields():
    for name, cls in AGENT_CLASSES.items():
        agent = cls()
        assert agent.agent_name == name
        assert agent.display_name
        assert agent.description
        assert agent.assigned_skills


def test_build_capabilities_prompt():
    prompt = build_capabilities_prompt()
    assert "technical_analyst" in prompt
    assert "K线技术分析师" in prompt
    assert "mx_data" in prompt

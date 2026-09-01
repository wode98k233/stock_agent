import pytest
from agents.group.state import (
    AgentStep, GroupState, SubAgentOutput, Request,
)


def test_agent_step_defaults():
    step = AgentStep(
        step=1, agent_names=["technical_analyst"],
        task_purpose="分析K线", input_params={},
        status="pending", result="", feedback="",
        requests=[], retry_count=0, executed_at="",
    )
    assert step["step"] == 1
    assert step["status"] == "pending"
    assert step["requests"] == []


def test_sub_agent_output_need_info():
    output = SubAgentOutput(
        result="需要板块数据",
        status="need_info",
        feedback="需要传导链确认",
        requests=[Request(
            type="agent_output", target="chain_analyst",
            description="获取板块资金流向", required=True,
        )],
    )
    assert output["status"] == "need_info"
    assert len(output["requests"]) == 1
    assert output["requests"][0]["target"] == "chain_analyst"


def test_group_state_serializable():
    state = GroupState(
        input="分析茅台",
        plan_steps=[], current_step_index=0,
        original_plan=[], accumulated_data="",
        resolved_data="", constraints="", observation="",
        response="", budget_exempt=None, _replan_count=0,
        _failed_agents=None, template_id=None, tool_calls=[],
        selected_skills=[], current_agent_names=[], current_task_purpose="",
    )
    assert state["input"] == "分析茅台"
    assert state["accumulated_data"] == ""

"""Dispatcher 路由测试 — Supervisor 模式"""
import copy
from types import SimpleNamespace

import pytest
from agents.group.state import AgentStep, GroupState
from agents.group.dispatcher import (
    _route_after_classifier,
    _route_after_supervisor,
    parallel_executor_node,
)


def _make_state(**overrides):
    defaults = dict(
        input="分析茅台", plan_steps=[], current_step_index=0,
        original_plan=[], accumulated_data="", resolved_data="",
        constraints="", observation="", response="", budget_exempt=None,
        _replan_count=0, _failed_agents=None, template_id=None,
        tool_calls=[], selected_skills=[], current_agent_names=[], current_task_purpose="",
        dispatch_history=[],
    )
    defaults.update(overrides)
    return GroupState(**defaults)


# ── classifier 路由 ──

def test_route_classifier_non_stock():
    state = _make_state(response="这不是股票问题")
    assert _route_after_classifier(state) == "end"


def test_route_classifier_stock():
    state = _make_state()
    assert _route_after_classifier(state) == "supervisor"


# ── supervisor 路由 ──

def test_route_supervisor_has_agent():
    """supervisor 决定调用 agent，路由到具体 agent 节点"""
    state = _make_state(current_agent_names=["technical_analyst"])
    assert _route_after_supervisor(state) == "technical_analyst"


def test_route_supervisor_no_agent():
    """supervisor 没有要调用的 agent，去报告"""
    state = _make_state(current_agent_names=[])
    assert _route_after_supervisor(state) == "report"


def test_route_supervisor_early_stop():
    """supervisor 判定提前终止"""
    state = _make_state(observation="early_stop")
    assert _route_after_supervisor(state) == "report"


def test_route_supervisor_error():
    """supervisor 出错，走 exception_summary"""
    state = _make_state(observation="error")
    assert _route_after_supervisor(state) == "exception_summary"


def _step(number, agents, purpose):
    return {
        "step": number,
        "agent_names": agents,
        "task_purpose": purpose,
        "input_params": {},
        "status": "pending",
        "result": "",
        "feedback": "",
        "requests": [],
        "retry_count": 0,
        "executed_at": "",
        "run_group": 1,
        "depends_on": [],
    }


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FakeAgent:
    def __init__(self, name, seen, *, fail=False):
        self.name = name
        self.seen = seen
        self.fail = fail

    async def __call__(self, state, config):
        if self.fail:
            raise RuntimeError(f"{self.name} failed")
        index = state["current_step_index"]
        self.seen[self.name] = (index, state["current_task_purpose"])
        result = copy.deepcopy(state)
        result["plan_steps"][index]["status"] = "success"
        result["plan_steps"][index]["result"] = f"{self.name} result"
        result["accumulated_data"] += f"\n\n{self.name} accumulated"
        result["tool_calls"].append({"tool_name": self.name})
        result["dispatch_history"].append({"dispatch": {"name": self.name}})
        return result


def _parallel_context():
    return SimpleNamespace(
        logger=_Logger(),
        budget=object(),
        skill_registry=object(),
        progress_reporter=None,
    )


@pytest.mark.asyncio
async def test_parallel_executor_binds_agents_to_steps_and_merges_only_deltas(monkeypatch):
    import agents.group.dispatcher as dispatcher

    seen = {}
    agents = {
        "agent_a": _FakeAgent("agent_a", seen),
        "agent_b": _FakeAgent("agent_b", seen),
    }
    monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
    state = _make_state(
        plan_steps=[
            _step(1, ["agent_a"], "purpose a"),
            _step(2, ["agent_b"], "purpose b"),
        ],
        current_agent_names=["agent_a", "agent_b"],
        current_task_purpose="fallback",
        accumulated_data="base",
        tool_calls=[{"tool_name": "base"}],
        dispatch_history=[{"dispatch": {"name": "base"}}],
    )

    result = await parallel_executor_node(state, _parallel_context())

    assert seen == {
        "agent_a": (0, "purpose a"),
        "agent_b": (1, "purpose b"),
    }
    assert [step["result"] for step in result["plan_steps"]] == [
        "agent_a result",
        "agent_b result",
    ]
    assert result["current_step_index"] == 2
    assert result["accumulated_data"].count("base") == 1
    assert [call["tool_name"] for call in result["tool_calls"]] == [
        "base",
        "agent_a",
        "agent_b",
    ]
    assert len(result["dispatch_history"]) == 3


@pytest.mark.asyncio
async def test_parallel_executor_stops_at_first_unfinished_step(monkeypatch):
    import agents.group.dispatcher as dispatcher

    seen = {}
    agents = {
        "agent_a": _FakeAgent("agent_a", seen),
        "agent_b": _FakeAgent("agent_b", seen, fail=True),
    }
    monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
    state = _make_state(
        plan_steps=[
            _step(1, ["agent_a"], "purpose a"),
            _step(2, ["agent_b"], "purpose b"),
        ],
        current_agent_names=["agent_a", "agent_b"],
    )

    result = await parallel_executor_node(state, _parallel_context())

    assert result["plan_steps"][0]["status"] == "success"
    assert result["plan_steps"][1]["status"] == "pending"
    assert result["current_step_index"] == 1


@pytest.mark.asyncio
async def test_parallel_executor_combines_agents_for_same_step(monkeypatch):
    import agents.group.dispatcher as dispatcher

    seen = {}
    agents = {
        "agent_a": _FakeAgent("agent_a", seen),
        "agent_b": _FakeAgent("agent_b", seen),
    }
    monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
    state = _make_state(
        plan_steps=[_step(1, ["agent_a", "agent_b"], "shared purpose")],
        current_agent_names=["agent_a", "agent_b"],
    )

    result = await parallel_executor_node(state, _parallel_context())

    assert seen == {
        "agent_a": (0, "shared purpose"),
        "agent_b": (0, "shared purpose"),
    }
    assert "agent_a result" in result["plan_steps"][0]["result"]
    assert "agent_b result" in result["plan_steps"][0]["result"]
    assert result["current_step_index"] == 1

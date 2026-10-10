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

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FakeAgent:
    def __init__(self, name, seen, *, fail=False):
        self.name = name
        self.seen = seen
        self.fail = fail

    def configure(self, llm, skill_registry, budget, logger, tool_rate_limiter=None):
        """PE._prepare 无条件 configure（2026-10-01 16:01 事故后强制要求）"""
        return self

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
    # 异常写回绑定步骤：pending → failed（supervisor 下轮可见，不再盲目重派）
    assert result["plan_steps"][1]["status"] == "failed"
    assert "agent_b failed" in result["plan_steps"][1]["feedback"]
    assert result["plan_steps"][1]["retry_count"] == 1
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


# ── _wrap 调用约定回归（2026-10-01 线上事故）─────────────────────
# build_dispatch_graph._wrap 以 (state, ctx, config) 3 个位置参数调用节点函数。
# parallel_executor_node 曾声明 (state, ctx, **kwargs)（仅 2 个位置参数），
# 并行批次一触发即 TypeError "takes 2 positional arguments but 3 were given"，
# 且发生在计划执行末期，先前烧掉的数十万 token 全部报废。
# 单测直接以 2 参调用绕过了 _wrap，故此处按 _wrap 的真实调用方式锁定。

def test_all_wrapped_nodes_accept_three_positional_args():
    """_wrap 包裹的每个节点函数必须接受 (state, ctx, config) 3 个位置参数"""
    import inspect

    from agents.group.nodes.classifier import classifier_node
    from agents.group.nodes.exception_summary import exception_summary_node
    from agents.group.nodes.report_enhance import report_enhance_node
    from agents.group.nodes.supervisor import supervisor_node

    for fn in (
        classifier_node,
        supervisor_node,
        parallel_executor_node,
        report_enhance_node,
        exception_summary_node,
    ):
        sig = inspect.signature(fn)
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        has_var_positional = any(
            p.kind == p.VAR_POSITIONAL for p in sig.parameters.values()
        )
        assert len(positional) >= 3 or has_var_positional, (
            f"{fn.__name__} 仅接受 {len(positional)} 个位置参数且无 *args，"
            f"会被 _wrap 的 3 参调用打崩（参照 2026-10-01 parallel_executor 事故）"
        )


@pytest.mark.asyncio
async def test_parallel_executor_survives_wrap_style_call(monkeypatch):
    """按 build_dispatch_graph._wrap 的真实调用方式（3 个位置参数）执行并行节点"""
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
    )

    # _wrap: await fn(state, ctx, config) — 3 个位置参数
    result = await parallel_executor_node(state, _parallel_context(), {"configurable": {}})

    assert seen == {
        "agent_a": (0, "purpose a"),
        "agent_b": (1, "purpose b"),
    }
    assert result["current_step_index"] == 2


# ── 重跑结果保护（2026-10-01 事故：空 result 覆盖数据底版）─────

class _EmptyResultAgent:
    """返回空 result 的假 agent（模拟补采被预算熔断）"""

    def __init__(self, name, seen, result=""):
        self.name = name
        self.seen = seen
        self._result = result

    def configure(self, *args, **kwargs):
        return self

    async def __call__(self, state, config):
        index = state["current_step_index"]
        self.seen[self.name] = index
        result = copy.deepcopy(state)
        result["plan_steps"][index]["status"] = "success"
        result["plan_steps"][index]["result"] = self._result
        result["dispatch_history"].append({"dispatch": {"name": self.name}})
        return result


async def _run_pe(monkeypatch, name, plan_step_result, agent_result):
    import agents.group.dispatcher as dispatcher

    seen = {}
    agents = {name: _EmptyResultAgent(name, seen, agent_result)}
    monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
    state = _make_state(
        plan_steps=[_step(1, [name], "补采")],
        current_agent_names=[name],
        current_task_purpose="补采",
    )
    state["plan_steps"][0]["result"] = plan_step_result
    if plan_step_result:
        state["plan_steps"][0]["status"] = "partial"
    return await parallel_executor_node(state, _parallel_context())


@pytest.mark.asyncio
async def test_parallel_executor_empty_result_keeps_existing_result(monkeypatch):
    """本次 result 为空 → 保留既有非空结果，只同步状态类字段"""
    result = await _run_pe(monkeypatch, "collector", "数据底版：深成-8.044%", "")

    step = result["plan_steps"][0]
    assert step["result"] == "数据底版：深成-8.044%"
    assert step["status"] == "success"


@pytest.mark.asyncio
async def test_parallel_executor_nonempty_result_overwrites(monkeypatch):
    """本次 result 非空 → 正常覆盖（防止保护过头）"""
    result = await _run_pe(monkeypatch, "collector", "旧结果", "新的补采结果")

    assert result["plan_steps"][0]["result"] == "新的补采结果"


@pytest.mark.asyncio
async def test_parallel_executor_empty_result_written_when_no_existing(monkeypatch):
    """既有结果也为空 → 正常写回空结果（不误判为需保护）"""
    result = await _run_pe(monkeypatch, "collector", "", "")

    assert result["plan_steps"][0]["result"] == ""
    assert result["plan_steps"][0]["status"] == "success"

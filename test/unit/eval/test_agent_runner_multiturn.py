"""AgentRunner 多轮执行单测（fake agent，验证轮次传递与结果装配）。

设计文档 §3：题库 case 支持 turns_json 多轮脚本（null 或长度 1 即单轮），
AgentRunner 在同一 dialog 上下文（同一 memory 实例）逐轮执行。
"""
import json

import pytest

from eval.runners import agent_runner as ar
from eval.runners.agent_runner import AgentRunner, parse_case_turns


class FakeAgent:
    """记录每次 run 的入参；fail_on=N 时第 N 次调用抛错。"""

    def __init__(self, fail_on: int | None = None):
        self.calls = []
        self.fail_on = fail_on

    def on_startup(self):
        pass

    async def run(self, user_input, registry, memory, logger, progress_callback=None):
        self.calls.append({"question": user_input, "registry": registry, "memory": memory})
        if self.fail_on and len(self.calls) == self.fail_on:
            raise RuntimeError("第2轮 boom")
        return f"answer:{user_input}"


@pytest.fixture
def runner(monkeypatch):
    r = AgentRunner(agent_mode="react_stock")
    fake = FakeAgent()
    registry_sentinel = object()
    monkeypatch.setattr(r, "_init_context", lambda: None)
    r._logger = object()
    r._memory = object()
    r._skill_register = object()
    monkeypatch.setattr(ar.AgentFactory, "get", lambda mode: fake)
    monkeypatch.setattr(ar, "SkillRegistry", lambda *a, **k: registry_sentinel)
    monkeypatch.setattr(ar, "find_latest_trace_run_id", lambda: None)
    r._fake_agent = fake
    r._fake_registry = registry_sentinel
    return r


def _case(**overrides):
    case = {"id": "c1", "question": "单轮问题", "expected_tools": ["t1"]}
    case.update(overrides)
    return case


# ── parse_case_turns ─────────────────────────────────────────────────

def test_parse_case_turns_fallback_to_single():
    turns = parse_case_turns(_case())
    assert len(turns) == 1
    assert turns[0]["turn_index"] == 1
    assert turns[0]["question"] == "单轮问题"
    assert turns[0]["expected_tools"] == ["t1"]


def test_parse_case_turns_single_scripted_turn_kept():
    """长度 1 的脚本轮原样保留（不回退到顶层字段）。"""
    script = [{"question": "脚本轮", "expected_tools": ["t2"]}]
    turns = parse_case_turns(_case(turns_json=json.dumps(script)))
    assert turns == [{
        "turn_index": 1,
        "question": "脚本轮",
        "expected_tools": ["t2"],
        "expected_sections": [],
    }]


def test_parse_case_turns_json_string_skips_invalid_items():
    script = [
        {"question": "第一轮", "expected_tools": ["a"], "expected_sections": ["s1"]},
        {"question": "第二轮"},
        {"question": "   "},   # 空问题轮跳过
        "not-a-dict",
    ]
    turns = parse_case_turns(_case(turns_json=json.dumps(script)))
    assert [t["question"] for t in turns] == ["第一轮", "第二轮"]
    assert [t["turn_index"] for t in turns] == [1, 2]
    assert turns[0]["expected_sections"] == ["s1"]
    assert turns[1]["expected_tools"] == []


def test_parse_case_turns_invalid_json_falls_back():
    turns = parse_case_turns(_case(turns_json="{bad json"))
    assert len(turns) == 1
    assert turns[0]["question"] == "单轮问题"


def test_parse_case_turns_empty_script_falls_back():
    assert len(parse_case_turns(_case(turns_json=json.dumps([])))) == 1


def test_parse_case_turns_no_question_at_all():
    assert parse_case_turns({"id": "c1", "question": ""}) == []


# ── run_single 多轮编排 ──────────────────────────────────────────────

def test_run_single_single_turn_shape(runner):
    data = runner.run_single(_case())
    assert data["status"] == "success"
    assert data["agent_response"] == "answer:单轮问题"
    assert data["benchmark_id"] == "c1"
    # 单轮不附加 turns 键，形状与历史一致
    assert "turns" not in data
    assert len(runner._fake_agent.calls) == 1


def test_run_single_multiturn_order_and_shared_context(runner):
    script = [{"question": f"轮{i}"} for i in (1, 2, 3)]
    data = runner.run_single(_case(turns_json=json.dumps(script)))

    assert data["status"] == "success"
    assert [c["question"] for c in runner._fake_agent.calls] == ["轮1", "轮2", "轮3"]
    # 同一 dialog 上下文：memory 与 registry 实例逐轮一致
    assert all(c["memory"] is runner._memory for c in runner._fake_agent.calls)
    assert all(c["registry"] is runner._fake_registry for c in runner._fake_agent.calls)
    # 逐轮结果 + 顶层聚合
    assert [t["turn_index"] for t in data["turns"]] == [1, 2, 3]
    assert [t["answer"] for t in data["turns"]] == ["answer:轮1", "answer:轮2", "answer:轮3"]
    assert data["agent_response"] == "answer:轮1\n\nanswer:轮2\n\nanswer:轮3"


def test_run_single_multiturn_error_stops(runner, monkeypatch):
    failing = FakeAgent(fail_on=2)
    monkeypatch.setattr(ar.AgentFactory, "get", lambda mode: failing)
    script = [{"question": "甲"}, {"question": "乙"}, {"question": "丙"}]

    data = runner.run_single(_case(turns_json=json.dumps(script)))

    assert data["status"] == "error"
    assert "boom" in data["error"]
    # 对话上下文已断：第 3 轮不执行
    assert [c["question"] for c in failing.calls] == ["甲", "乙"]
    assert [t["turn_index"] for t in data["turns"]] == [1, 2]
    assert data["turns"][-1]["status"] == "error"
    assert data["agent_response"] == "answer:甲"


def test_run_single_empty_turns_is_error(runner):
    data = runner.run_single({"id": "c1", "question": "  "})
    assert data["status"] == "error"
    assert "无可用轮次" in data["error"]

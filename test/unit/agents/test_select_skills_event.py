"""SelectSkillsNode 在选择完成后写入技能事件（recorder 可缺失）。"""
import asyncio
import logging

from agents.react.nodes import SelectSkillsNode


class _StubRegistry:
    def get_skill_catalog(self):
        return []

    def get_tools(self, name):
        return []

    def get_all_tools(self):
        return []


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def record_skill_event(self, kind, data):
        self.events.append((kind, data))
        return 1


class _BrokenRecorder:
    def record_skill_event(self, kind, data):
        raise RuntimeError("recorder boom")


def _run_node(monkeypatch, recorder):
    monkeypatch.setattr(
        "agents.react.nodes.llm_json_with_retry",
        lambda *a, **k: {"selected_skills": ["technical_analysis"], "reason": "需要技术指标"},
    )
    monkeypatch.setattr("agents.react.nodes.get_active_recorder", lambda: recorder)
    node = SelectSkillsNode(
        llm=object(), skill_registry=_StubRegistry(), logger=logging.getLogger("test_select_skills")
    )
    state = {"user_input": "分析贵州茅台", "selected_template": None}
    return asyncio.run(node(state))


def test_select_skills_records_event(monkeypatch):
    fake = _FakeRecorder()
    out = _run_node(monkeypatch, fake)
    assert out["selected_skills"] == ["technical_analysis"]
    assert fake.events and fake.events[0][0] == "select"
    assert fake.events[0][1]["selected"] == ["technical_analysis"]
    assert fake.events[0][1]["reason"] == "需要技术指标"
    assert fake.events[0][1]["market"] == "unknown"
    assert fake.events[0][1]["tools"] == []
    assert fake.events[0][1]["active_skill"] == "technical_analysis"
    assert fake.events[0][1]["candidates"] == ["technical_analysis"]


def test_select_skills_without_recorder(monkeypatch):
    out = _run_node(monkeypatch, None)
    assert out["selected_skills"] == ["technical_analysis"]


def test_select_skills_recorder_failure_does_not_break(monkeypatch):
    out = _run_node(monkeypatch, _BrokenRecorder())
    assert out["selected_skills"] == ["technical_analysis"]


def test_select_skills_preserves_reason_on_invalid_selection(monkeypatch):
    fake = _FakeRecorder()
    monkeypatch.setattr(
        "agents.react.nodes.llm_json_with_retry",
        lambda *a, **k: {"reason": "仅返回理由"},
    )
    monkeypatch.setattr("agents.react.nodes.get_active_recorder", lambda: fake)
    node = SelectSkillsNode(
        llm=object(), skill_registry=_StubRegistry(), logger=logging.getLogger("test_select_skills")
    )
    out = asyncio.run(node({"user_input": "分析贵州茅台", "selected_template": None}))
    assert out["selected_skills"] == []
    assert out["active_skill"] == ""
    assert out["candidate_skills"] == []
    assert fake.events[0][1]["active_skill"] == ""
    assert fake.events[0][1]["candidates"] == []
    assert fake.events[0][1]["reason"] == "仅返回理由"

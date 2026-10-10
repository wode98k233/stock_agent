"""PDOR executor 技能激活事件。"""
from agents.pdor.node import executor as ex


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def record_skill_event(self, kind, data):
        self.events.append((kind, data))


def test_pdor_executor_records_activate(monkeypatch):
    fake = _FakeRecorder()
    monkeypatch.setattr("agents.pdor.node.executor.get_active_recorder", lambda: fake)
    ex._record_skill_activate("mx_data", ["mx_data_query"], "pdor_step", 2)
    assert fake.events == [("activate", {
        "active_skill": "mx_data",
        "source": "pdor_step",
        "tools": ["mx_data_query"],
        "step": 2,
    })]


def test_pdor_executor_records_activate_without_recorder(monkeypatch):
    monkeypatch.setattr("agents.pdor.node.executor.get_active_recorder", lambda: None)
    ex._record_skill_activate("mx_data", ["mx_data_query"], "pdor_step", 2)

"""历史消息清洗：图表 UUID / 仪表盘 JSON 不应进入 prompt（不联网）。"""
from langchain_core.messages import AIMessage

from agents.react.utils import clean_history_messages
from utils.memory import strip_history_markers


def test_strip_removes_dashboard_block():
    text = "报告正文\n@@DASHBOARD_START@@{\"a\":1}@@DASHBOARD_END@@"
    assert strip_history_markers(text) == "报告正文"


def test_strip_removes_chart_markers():
    text = "结论\n@@CHART:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b@@\n免责声明"
    out = strip_history_markers(text)
    assert "@@CHART" not in out
    assert "结论" in out and "免责声明" in out


def test_strip_keeps_plain_text():
    assert strip_history_markers("普通文本，没有标记") == "普通文本，没有标记"
    assert strip_history_markers("") == ""


def test_clean_history_strips_ai_message():
    ai = AIMessage(content="正文@@DASHBOARD_START@@{}@@DASHBOARD_END@@")
    out = clean_history_messages([ai], "当前问题")
    assert len(out) == 1
    assert out[0].content == "正文"


def test_clean_history_strips_tuple():
    history = [("user", "上一个问题"), ("assistant", "报告@@CHART:abc-123@@")]
    out = clean_history_messages(history, "当前问题")
    assert out[0] == ("user", "上一个问题")
    assert "@@CHART" not in out[1][1]


def test_replay_history_strips_markers_before_memory():
    from server.agent_runner import WebAgentContext

    class _Memory:
        enabled = True
        turns = []

        def append_turn(self, user, ai):
            self.turns.append((user, ai))

    class _Storage:
        def list_messages(self, dialog_uuid):
            return [
                {"role": "user", "content": "问题1"},
                {"role": "assistant", "status": "completed",
                 "content": "报告1@@DASHBOARD_START@@{\"x\":1}@@DASHBOARD_END@@"},
                {"role": "user", "content": "问题2"},
                {"role": "assistant", "status": "completed", "content": "报告2"},
            ]

    mem = _Memory()
    WebAgentContext._replay_history(mem, "dlg", _Storage())
    assert mem.turns == [("问题1", "报告1"), ("问题2", "报告2")]

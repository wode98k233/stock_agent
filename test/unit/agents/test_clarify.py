"""agents.shared.clarify 单测（离线，不调用真实 LLM）。"""
import asyncio
import json

import pytest

from agents.shared import clarify as clarify_mod
from agents.shared.budget_ctx import budget_decision_ctx
from utils.cancellation import AgentCancelled

_QUESTIONS = [{
    "id": "q1",
    "question": "你打算投入多少本金？",
    "options": [
        {"label": "10万以内", "value": "10万以内"},
        {"label": "10-50万", "value": "10-50万"},
    ],
    "recommended": "10-50万",
    "allow_custom": True,
}]


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def _run(coro):
    return asyncio.run(coro)


def test_no_web_channel_skips(monkeypatch):
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    assert _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger())) == {}


def test_disabled_skips(monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "CLARIFY_ENABLED", False, raising=False)
    token = budget_decision_ctx.set(lambda payload: json.dumps({"answers": {"q1": "x"}}))
    try:
        assert _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger())) == {}
    finally:
        budget_decision_ctx.reset(token)


def test_no_questions_skips(monkeypatch):
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: [])
    token = budget_decision_ctx.set(lambda payload: "{}")
    try:
        assert _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger())) == {}
    finally:
        budget_decision_ctx.reset(token)


def test_answers_enrich_input(monkeypatch):
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    captured = {}

    def cb(payload):
        captured.update(payload)
        return json.dumps({"answers": {"q1": "20万"}})

    token = budget_decision_ctx.set(cb)
    try:
        out = _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)

    assert captured["event_name"] == "clarify_required"
    assert captured["original_question"] == "分析茅台"
    assert out["answers"] == {"q1": "20万"}
    assert "【用户补充信息（澄清回答）】" in out["enriched_input"]
    assert "20万" in out["enriched_input"]


def test_skipped_question_not_injected(monkeypatch):
    """跳过的题不进补充分析：AI 按「用户没提供这个信息」处理。"""
    q2 = [dict(_QUESTIONS[0]),
          {"id": "q2", "question": "投资期限多长？",
           "options": [{"label": "1年以内", "value": "1年以内"}],
           "recommended": "1年以内", "allow_custom": True}]
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: q2)
    token = budget_decision_ctx.set(
        lambda payload: json.dumps({"answers": {"q1": "20万"}, "skipped": ["q2"]}))
    try:
        out = _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)

    assert "20万" in out["enriched_input"]
    assert "投资期限多长" not in out["enriched_input"]
    assert out["answers"] == {"q1": "20万"}


def test_all_skipped_returns_empty(monkeypatch):
    """全部跳过 = 没有可注入的补充信息 → 零打扰放行（不改写输入）。"""
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    token = budget_decision_ctx.set(
        lambda payload: json.dumps({"answers": {}, "skipped": ["q1"]}))
    try:
        assert _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger())) == {}
    finally:
        budget_decision_ctx.reset(token)


def test_records_clarify_step_in_trace(monkeypatch):
    """澄清交互要落 trace（step_type=clarify）：耗时被剔除后仍能看出等了多久。"""
    from utils.agent_trace.core import TraceRecorder, _active_recorder

    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    rec = TraceRecorder(db_path=":memory:")
    rec.start_run("分析茅台")
    token_rec = _active_recorder.set(rec)
    token = budget_decision_ctx.set(lambda payload: json.dumps({"answers": {"q1": "20万"}}))
    try:
        _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)
        _active_recorder.reset(token_rec)

    try:
        conn = rec._get_conn()
        rows = conn.execute(
            "SELECT step_type, status, extra FROM steps WHERE step_type='clarify'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "success"
        extra = json.loads(rows[0]["extra"])
        assert extra["outcome"] == "answered"
        assert extra["answered"] == 1
        assert extra["question_count"] == 1
        assert extra["wait_s"] >= 0
    finally:
        rec.close()


def test_rejected_returns_response(monkeypatch):
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    token = budget_decision_ctx.set(lambda payload: json.dumps({"rejected": True}))
    try:
        out = _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)
    assert out["rejected"] is True
    assert out["response"]


def test_timeout_defaults_to_recommended(monkeypatch):
    """澄清等待超时 → 按推荐选项继续，而不是把任务判死。"""
    from agents.shared.clarify import CLARIFY_TIMEOUT

    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    token = budget_decision_ctx.set(lambda payload: CLARIFY_TIMEOUT)
    try:
        out = _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)

    assert out["timed_out"] is True
    assert out["answers"] == {"q1": "10-50万"}
    assert "10-50万" in out["enriched_input"]
    assert "【用户补充信息（澄清回答）】" in out["enriched_input"]


def test_payload_carries_countdown_fields(monkeypatch):
    """payload 要带 timeout_s / deadline_ms，前端倒计时条与刷新补弹窗都靠它。"""
    captured = {}

    def cb(payload):
        captured.update(payload)
        return json.dumps({"answers": {"q1": "20万"}})

    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    token = budget_decision_ctx.set(cb)
    try:
        _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)

    import time as _time
    assert captured["timeout_s"] >= 30
    remaining = captured["deadline_ms"] - _time.time() * 1000
    assert 0 < remaining <= captured["timeout_s"] * 1000 + 100


def test_clarify_wait_excluded_from_budget(monkeypatch):
    """等用户作答的阻塞时间必须从预算耗时里扣除（否则 5 分钟等待吃光时间预算）。"""
    import time as _time

    class _Budget:
        def __init__(self):
            self.excluded = []

        def exclude_seconds(self, seconds):
            self.excluded.append(seconds)

    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    budget = _Budget()

    def cb(payload):
        _time.sleep(0.05)
        return json.dumps({"answers": {"q1": "20万"}})

    token = budget_decision_ctx.set(cb)
    try:
        _run(clarify_mod.maybe_clarify(
            "分析茅台", None, None, _Logger(), budget=budget))
    finally:
        budget_decision_ctx.reset(token)

    assert budget.excluded and budget.excluded[0] >= 0.04


def test_cancel_raises_agent_cancelled(monkeypatch):
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    token = budget_decision_ctx.set(lambda payload: "cancel")
    try:
        with pytest.raises(AgentCancelled):
            _run(clarify_mod.maybe_clarify("分析茅台", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)


def test_original_question_prefers_raw_input_ctx(monkeypatch):
    """payload.original_question 必须是用户原话（enrich 前的 ContextVar），
    否则前端「重新描述」会把画像/记忆整段回填进输入框。"""
    from agents.shared.raw_input_ctx import reset_raw_input, set_raw_input

    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    captured = {}

    def cb(payload):
        captured.update(payload)
        return json.dumps({"rejected": True})

    token = budget_decision_ctx.set(cb)
    raw_tok = set_raw_input("帮我配置一下仓位")
    try:
        _run(clarify_mod.maybe_clarify("[背景信息]…一大段 enrich 文本", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)
        reset_raw_input(raw_tok)

    assert captured["original_question"] == "帮我配置一下仓位"


def test_clarify_prompt_renders_without_nested_brace_error():
    """回归：CLARIFY_PROMPT 只能由 LangChain 渲染一次（JSON 花括号已转义），
    不能再手动 .format()；否则报 'Nested replacement fields are not allowed'
    并静默跳过澄清（2026-10-09 实测：clarify 从未真正提问过）。"""
    from langchain_core.prompts import ChatPromptTemplate
    from agents.prompts import CLARIFY_PROMPT

    msgs = ChatPromptTemplate.from_messages([
        ("system", CLARIFY_PROMPT),
        ("user", "{input}"),
    ]).format_messages(input="测试", max_q=3)
    content = msgs[0].content
    assert "3" in content
    assert '"questions"' in content
    assert '"recommended"' in content


def test_resolve_answer_text_maps_value_to_label():
    """LLM 把 value 写成英文 key 时，展示/注入必须用中文 label。"""
    q = {"options": [{"label": "盘中即时快照", "value": "intraday_snapshot"}]}
    assert clarify_mod.resolve_answer_text(q, "intraday_snapshot") == "盘中即时快照"
    assert clarify_mod.resolve_answer_text(q, "自定义内容") == "自定义内容"
    assert clarify_mod.resolve_answer_text(q, "") == ""


def test_enriched_input_uses_label_not_english_value(monkeypatch):
    q_en = [{
        "id": "q1", "question": "按哪种口径？",
        "options": [
            {"label": "盘中即时快照", "value": "intraday_snapshot"},
            {"label": "收盘完整复盘", "value": "close_review"},
        ],
        "recommended": "intraday_snapshot", "allow_custom": True,
    }]
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: q_en)
    token = budget_decision_ctx.set(
        lambda p: json.dumps({"answers": {"q1": "intraday_snapshot"}}))
    try:
        out = _run(clarify_mod.maybe_clarify("今天大盘怎么样", None, None, _Logger()))
    finally:
        budget_decision_ctx.reset(token)
    assert "盘中即时快照" in out["enriched_input"]
    assert "intraday_snapshot" not in out["enriched_input"]


def test_format_clarify_answers_uses_label():
    """落库的用户作答文本同样用 label，不出现英文 value。"""
    from server.routes.tasks import _format_clarify_answers

    qs = [{"id": "q1", "question": "按哪种口径？",
           "options": [{"label": "盘中即时快照", "value": "intraday_snapshot"}]}]
    text = _format_clarify_answers(qs, {"q1": "intraday_snapshot"})
    assert "盘中即时快照" in text
    assert "intraday_snapshot" not in text


def test_validate_questions_normalizes():
    out = clarify_mod._validate_questions([
        {"question": "Q1", "options": ["a", {"label": "b", "value": "B"}], "recommended": "nope"},
        {"question": "", "options": []},
        "junk",
    ], 3)
    assert len(out) == 1
    assert out[0]["id"] == "q1"
    assert out[0]["recommended"] == "a"
    assert out[0]["options"][1]["value"] == "B"
    assert out[0]["allow_custom"] is True

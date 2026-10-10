"""engine：run 级评审编排（成功落库 / 失败兜底 / 缺失 trace / 终态契约）。"""
import json
import sqlite3

import pytest

from utils.agent_trace.models import ensure_full_schema
from eval.review import engine, rubrics

_ANSWER = "综合来看该股上行空间明确"
_OK_RESULT = {
    "overall": 88.0,
    "dimensions": {"risk_disclosure": 88.0},
    "findings": [{"step_ref": 1, "dimension": "risk_disclosure", "verdict": "v",
                  "evidence": [{"source": "answer", "step_ref": 0, "quote": _ANSWER}]}],
    "capabilities_required": ["行情"],
    "summary": "ok",
}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    c.row_factory = sqlite3.Row
    ensure_full_schema(c)
    c.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) "
        "VALUES('r1','react_stock',?,?, 'success','2026-09-18T01:00:00')",
        (json.dumps("问题", ensure_ascii=False),
         json.dumps(_ANSWER, ensure_ascii=False)),
    )
    c.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at) "
        "VALUES('r1','e1','tool','t',?,'success','2026-09-18T01:00:01')",
        (json.dumps({"a": 1}),),
    )
    c.commit()
    # 分库后 engine 的 trace 读取走 _trace_conn（月库路由）；测试注入同一临时连接
    monkeypatch.setattr(engine, "_trace_conn", lambda run_id: c)
    yield c
    c.close()


def test_review_run_done(conn, monkeypatch):
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: dict(_OK_RESULT))
    out = engine.review_run(conn, "r1")
    assert out["status"] == "done"
    assert out["overall"] == 88.0
    assert out["findings"][0]["step_ref"] == 1
    assert out["rubric_version"] == rubrics.RUBRIC_VERSION
    assert out["judge_model"] == engine.judge_model_name()
    assert out["duration_ms"] is not None and out["duration_ms"] >= 0
    assert out["inputs"]["run_id"] == "r1"
    assert out["inputs"]["summary"] == "ok"
    assert out["inputs"]["capabilities_required"] == ["行情"]


def test_review_run_failure_marks_failed(conn, monkeypatch):
    def _boom(digest, llm=None):
        raise RuntimeError("judge down")

    monkeypatch.setattr(engine, "judge_run", _boom)
    out = engine.review_run(conn, "r1")
    assert out["status"] == "failed"
    assert "judge down" in out["error"]


def test_review_run_missing_trace_marks_failed(conn, monkeypatch):
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: {})
    out = engine.review_run(conn, "nope")
    assert out["status"] == "failed"
    assert "trace run 不存在" in out["error"]


# ── 证据引用校验接线 ──────────────────────────────────────────────────
def _bad_quote_result(quote="完全不存在的一句话"):
    r = dict(_OK_RESULT)
    r["findings"] = [{"step_ref": 1, "dimension": "risk_disclosure", "verdict": "v",
                      "evidence": [{"source": "answer", "step_ref": 0, "quote": quote}]}]
    return r


def test_evidence_meta_recorded_and_no_retry_when_verified(conn, monkeypatch):
    calls = []

    def _judge(digest, llm=None, feedback=""):
        calls.append(feedback)
        return dict(_OK_RESULT)

    monkeypatch.setattr(engine, "judge_run", _judge)
    out = engine.review_run(conn, "r1")
    assert out["status"] == "done"
    meta = out["inputs"]["evidence"]
    assert meta["evidence_verified"] == 1 and meta["evidence_unverified"] == 0
    assert meta["evidence_retried"] is False
    assert len(calls) == 1                      # 未触发重试


def test_retry_with_feedback_adopted_when_better(conn, monkeypatch):
    """首次引用未命中 → 带反馈重试 → 重试命中则采纳重试结果。"""
    calls = []

    def _judge(digest, llm=None, feedback=""):
        calls.append(feedback)
        return dict(_bad_quote_result()) if len(calls) == 1 else dict(_OK_RESULT)

    monkeypatch.setattr(engine, "judge_run", _judge)
    out = engine.review_run(conn, "r1")
    assert len(calls) == 2
    assert "无法在给定数据中找到" in calls[1]     # 反馈里带上了未命中的引用
    assert out["inputs"]["evidence"]["evidence_retried"] is True
    assert out["inputs"]["evidence"]["evidence_retry_adopted"] is True
    assert out["inputs"]["evidence"]["evidence_unverified"] == 0
    assert out["findings"][0]["unverified"] is False


def test_retry_keeps_first_when_not_better(conn, monkeypatch):
    def _judge(digest, llm=None, feedback=""):
        return dict(_bad_quote_result())

    monkeypatch.setattr(engine, "judge_run", _judge)
    out = engine.review_run(conn, "r1")
    assert out["status"] == "done"
    # 降级保留而非丢弃
    assert len(out["findings"]) == 1
    assert out["findings"][0]["unverified"] is True
    assert out["inputs"]["evidence"]["evidence_unverified"] == 1
    assert out["inputs"]["evidence"]["evidence_retried"] is True
    assert out["inputs"]["evidence"]["evidence_retry_adopted"] is False  # 一样差不换


def test_retry_exception_keeps_first_result(conn, monkeypatch):
    """重试本身抛错不能阻断评审，也不该把记录打成 failed。"""
    calls = []

    def _judge(digest, llm=None, feedback=""):
        calls.append(feedback)
        if len(calls) > 1:
            raise RuntimeError("judge 二次调用挂了")
        return dict(_bad_quote_result())

    monkeypatch.setattr(engine, "judge_run", _judge)
    out = engine.review_run(conn, "r1")
    assert out["status"] == "done"
    assert out["findings"][0]["unverified"] is True


def test_unknown_dimension_triggers_retry(conn, monkeypatch):
    calls = []

    def _judge(digest, llm=None, feedback=""):
        calls.append(feedback)
        if len(calls) == 1:
            r = dict(_OK_RESULT)
            r["findings"] = [{"step_ref": 1, "dimension": "invented_dim",
                              "verdict": "v",
                              "evidence": [{"source": "answer", "quote": _ANSWER}]}]
            return r
        return dict(_OK_RESULT)

    monkeypatch.setattr(engine, "judge_run", _judge)
    out = engine.review_run(conn, "r1")
    assert len(calls) == 2
    assert "不在允许清单内" in calls[1]


def test_low_confidence_marked_when_low_score_without_verified_finding(conn, monkeypatch):
    r = dict(_bad_quote_result())
    r["dimensions"] = {"risk_disclosure": 30.0, "actionability": 95.0}
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None, feedback="": dict(r))
    out = engine.review_run(conn, "r1")
    lc = out["inputs"]["evidence"]["low_confidence"]
    assert lc["risk_disclosure"] is True       # 低分且无已核验依据
    assert lc["actionability"] is False        # 高分


def test_evidence_meta_present_on_dialog_review(conn, monkeypatch):
    from eval.review import store
    run_uuid = store.create_review(conn, "run", "r1", rubrics.RUBRIC_VERSION, "m")
    store.finish_review(conn, run_uuid, overall=90.0, scores={}, findings=[])
    monkeypatch.setattr(engine, "judge_dialog", _judge_dialog_stub)
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "r1"}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    assert "evidence" in out["inputs"]


def test_done_not_flipped_when_readback_fails(conn, monkeypatch):
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: dict(_OK_RESULT))

    def _read_boom(c, uuid):
        raise RuntimeError("readback down")

    monkeypatch.setattr(engine.store, "get_review", _read_boom)
    with pytest.raises(RuntimeError, match="readback down"):
        engine.review_run(conn, "r1")
    row = conn.execute(
        "SELECT status, error FROM reviews WHERE target_id='r1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "done"
    assert row["error"] is None


def test_fail_review_error_does_not_leak(conn, monkeypatch):
    def _judge(digest, llm=None):
        raise RuntimeError("judge down")

    def _fail_boom(c, review_uuid, error, duration_ms=None):
        raise sqlite3.OperationalError("db locked")

    monkeypatch.setattr(engine, "judge_run", _judge)
    monkeypatch.setattr(engine.store, "fail_review", _fail_boom)

    out = engine.review_run(conn, "r1")
    assert out["status"] == "running"
    assert out["error"] is None
    row = conn.execute("SELECT status FROM reviews WHERE target_id='r1'").fetchone()
    assert row["status"] == "running"


def test_judge_malformed_result_marks_failed(conn, monkeypatch):
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: {})
    out = engine.review_run(conn, "r1")
    assert out["status"] == "failed"
    assert "缺少字段" in out["error"]
    assert "overall" in out["error"]


def test_finish_miss_marks_failed(conn, monkeypatch):
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: dict(_OK_RESULT))
    monkeypatch.setattr(engine.store, "finish_review", lambda *args, **kwargs: False)
    out = engine.review_run(conn, "r1")
    assert out["status"] == "failed"
    assert "finish_review" in out["error"]


def test_readback_none_raises_runtime_error(conn, monkeypatch):
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: dict(_OK_RESULT))
    monkeypatch.setattr(engine.store, "get_review", lambda c, uuid: None)
    with pytest.raises(RuntimeError, match="读回为空"):
        engine.review_run(conn, "r1")


def test_find_active_running_fresh_and_stale(conn, monkeypatch):
    import json as _json
    from datetime import datetime, timedelta, timezone
    from eval.review import engine, store

    uuid = store.create_review(conn, "dialog", "d1", "v1", "m")
    active = engine.find_active_running(conn, "dialog", "d1")
    assert active and active["review_uuid"] == uuid

    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    conn.execute("UPDATE reviews SET created_at=? WHERE review_uuid=?", (old, uuid))
    conn.commit()
    assert engine.find_active_running(conn, "dialog", "d1") is None
    assert store.get_review(conn, uuid)["status"] == "failed"

    assert engine.find_active_running(conn, "dialog", "missing") is None


def test_find_active_running_custom_stale_seconds(conn):
    from datetime import datetime, timedelta, timezone
    from eval.review import engine, store

    uuid = store.create_review(conn, "dialog", "d1", "v1", "m")
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    conn.execute("UPDATE reviews SET created_at=? WHERE review_uuid=?", (old, uuid))
    conn.commit()

    active = engine.find_active_running(conn, "dialog", "d1", stale_seconds=600)
    assert active and active["review_uuid"] == uuid

    assert engine.find_active_running(conn, "dialog", "d1", stale_seconds=60) is None
    assert store.get_review(conn, uuid)["status"] == "failed"


class _FakeSource:
    def __init__(self, dialog, turns):
        self._dialog = dialog
        self._turns = turns

    def get_dialog(self, uuid):
        return self._dialog if self._dialog else None

    def list_turns(self, uuid):
        return self._turns


def _judge_dialog_stub(digest, llm=None):
    return {"overall": 77.0, "dimensions": {"consistency": 77.0},
            "findings": [{"turn_index": 1, "dimension": "consistency"}], "summary": "s"}


def test_review_dialog_reuses_done_run_review(conn, monkeypatch):
    from eval.review import engine, store
    # 预置一条**同版本**已完成的 run 评审，应被复用（版本无关：引用常量而非硬编码）
    run_uuid = store.create_review(conn, "run", "r1", rubrics.RUBRIC_VERSION, "m")
    store.finish_review(conn, run_uuid, overall=90.0, scores={}, findings=[])
    monkeypatch.setattr(engine, "judge_dialog", _judge_dialog_stub)
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "r1"}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    assert out["overall"] == 77.0
    assert out["rubric_version"] == rubrics.DIALOG_RUBRIC_VERSION
    assert out["inputs"]["turns"] == [{"turn_index": 1, "run_id": "r1", "review_id": run_uuid}]
    assert len(store.list_reviews(conn, "run", "r1")) == 1  # 复用了旧 run 评审


def test_review_dialog_does_not_reuse_other_version_run_review(conn, monkeypatch):
    """跨版本不复用：v1 的 run 评审不能拿来当 v2 的逐轮输入。"""
    from eval.review import engine, store
    stale = store.create_review(conn, "run", "r1", "v0-stale", "m")
    store.finish_review(conn, stale, overall=90.0, scores={}, findings=[])
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: dict(_OK_RESULT))
    monkeypatch.setattr(engine, "judge_dialog", _judge_dialog_stub)
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "r1"}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    used = out["inputs"]["turns"][0]["review_id"]
    assert used != stale                      # 新评了一次，没有复用旧版本
    assert len(store.list_reviews(conn, "run", "r1")) == 2


def test_review_dialog_missing_dialog_marks_failed(conn, monkeypatch):
    from eval.review import engine
    monkeypatch.setattr(engine, "judge_dialog", _judge_dialog_stub)
    src = _FakeSource(None, [])
    out = engine.review_dialog(conn, "nope", src)
    assert out["status"] == "failed"
    assert "dialog 不存在" in out["error"]


def test_review_dialog_degraded_turn_does_not_block(conn, monkeypatch):
    from eval.review import engine, store
    captured = {}

    def _capture(digest, llm=None):
        captured["digest"] = digest
        return _judge_dialog_stub(digest)

    monkeypatch.setattr(engine, "judge_dialog", _capture)
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "missing-run"},
                       {"turn_index": 2, "question": "q2", "answer": "a2", "run_id": ""}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    turns = captured["digest"]["turns"]
    assert turns[0]["review_status"] == "failed"
    assert turns[1]["review_status"] == "no_trace"


def test_ensure_run_review_reuses_active_running(conn):
    from eval.review import engine, store

    uuid = store.create_review(conn, "run", "r1", engine.RUBRIC_VERSION, "m")
    out = engine.ensure_run_review(conn, "r1")
    assert out["review_uuid"] == uuid
    assert out["status"] == "running"
    assert len(store.list_reviews(conn, "run", "r1")) == 1


def test_review_dialog_passes_run_review_error_into_summary(conn, monkeypatch):
    from eval.review import engine
    captured = {}

    def _capture(digest, llm=None):
        captured["digest"] = digest
        return _judge_dialog_stub(digest)

    monkeypatch.setattr(engine, "judge_dialog", _capture)
    monkeypatch.setattr(engine, "ensure_run_review", lambda c, run_id, llm=None: {
        "review_uuid": "u", "status": "failed", "error": "judge down"})
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "r1"}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    assert captured["digest"]["turns"][0]["error"] == "judge down"


def test_review_dialog_digest_carries_cross_turn(conn, monkeypatch):
    """二期：dialog digest 携带跨轮取证（§9），digest 与落库 inputs 都有 cross_turn。"""
    from eval.review import engine, store
    captured = {}

    def _capture(digest, llm=None):
        captured["digest"] = digest
        return _judge_dialog_stub(digest)

    monkeypatch.setattr(engine, "judge_dialog", _capture)
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None: dict(_OK_RESULT))
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "r1"},
                       {"turn_index": 2, "question": "q2", "answer": "a2", "run_id": "r1"}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    ct = captured["digest"]["cross_turn"]
    assert set(ct) == {"repeat_calls", "number_conflicts", "constraint_violations"}
    # conn 夹具给 r1 预置了一个 tool 步（'t'，args {"a":1}）→ 两轮同参 → 重复检出
    assert ct["repeat_calls"] and ct["repeat_calls"][0]["tool"] == "t"
    assert out["inputs"]["cross_turn"] == ct

"""review_service：状态组装、发起评审、后台任务。"""
import sqlite3

import pytest

from utils.db_schema.registry import ALL_DBS
from utils.db_schema.migrator import ensure_schema


class _FakeStorage:
    def __init__(self, dialog=None, messages=None):
        self._dialog = dialog
        self._messages = messages or []

    def get_dialog(self, uuid):
        return self._dialog if self._dialog and self._dialog.get("dialog_uuid") == uuid else None

    def list_messages(self, uuid):
        return self._messages


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    c.row_factory = sqlite3.Row
    d = ALL_DBS["agent_trace"]
    ensure_schema(c, d.tables, d.special_migrations)
    yield c
    c.close()


def _dialog():
    return {"dialog_uuid": "d1", "title": "复盘"}


def _messages():
    return [
        {"role": "user", "content": "q1", "trace_run_id": "", "created_at": "2026-09-18T10:00:00"},
        {"role": "assistant", "content": "a1", "trace_run_id": "r1", "created_at": "2026-09-18T10:00:01"},
        {"role": "user", "content": "q2", "trace_run_id": "", "created_at": "2026-09-18T10:01:00"},
        {"role": "assistant", "content": "a2", "trace_run_id": "r2", "created_at": "2026-09-18T10:01:01"},
    ]


def test_get_dialog_reviews_assembles(conn, monkeypatch):
    from eval.review import store
    from server import review_service

    store.create_review(conn, "dialog", "d1", "v1", "m")
    r1 = store.create_review(conn, "run", "r1", "v1", "m")
    store.finish_review(conn, r1, 80.0, {}, [])
    storage = _FakeStorage(_dialog(), _messages())
    out = review_service.get_dialog_reviews(storage, "d1", conn=conn)
    assert out["title"] == "复盘"
    assert [r["run_id"] for r in out["runs"]] == ["r1", "r2"]
    assert out["runs"][0]["review"]["overall"] == 80.0
    assert out["runs"][1]["review"] is None
    assert len(out["dialog_reviews"]) == 1


def test_get_dialog_reviews_missing_dialog(conn):
    from server import review_service
    with pytest.raises(ValueError):
        review_service.get_dialog_reviews(_FakeStorage(None), "nope", conn=conn)


def test_start_dialog_review_dedup_and_create(conn, monkeypatch):
    from eval.review import store
    from server import review_service

    monkeypatch.setattr(review_service, "get_trace_conn", lambda: conn)
    storage = _FakeStorage(_dialog(), _messages())
    out = review_service.start_dialog_review(storage, "d1")
    assert out["status"] == "running"
    first = out["review_uuid"]

    out2 = review_service.start_dialog_review(storage, "d1")
    assert "error" in out2 and out2["review_uuid"] == first

    store.fail_review(conn, first, "done")
    out3 = review_service.start_dialog_review(storage, "d1")
    assert out3["status"] == "running" and out3["review_uuid"] != first


def test_start_dialog_review_missing_dialog(conn, monkeypatch):
    from server import review_service
    monkeypatch.setattr(review_service, "get_trace_conn", lambda: conn)
    out = review_service.start_dialog_review(_FakeStorage(None), "nope")
    assert "error" in out


def test_run_dialog_review_task_marks_failed_on_error(conn, monkeypatch, tmp_path):
    from eval.review import engine as review_engine, store
    from server import review_service

    review_uuid = store.create_review(conn, "dialog", "d1", "v1", "m")

    monkeypatch.setattr(review_service, "get_trace_conn", lambda: conn)
    monkeypatch.setattr(review_engine, "execute_dialog",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    review_service.run_dialog_review_task(_FakeStorage(_dialog(), _messages()), "d1", review_uuid)
    assert store.get_review(conn, review_uuid)["status"] == "failed"


def test_start_dialog_review_keeps_running_within_dialog_stale(conn, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from eval.review import store
    from server import review_service

    review_uuid = store.create_review(conn, "dialog", "d1", "v1", "m")
    created = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    conn.execute("UPDATE reviews SET created_at=? WHERE review_uuid=?", (created, review_uuid))
    conn.commit()
    monkeypatch.setattr(review_service, "get_trace_conn", lambda: conn)
    out = review_service.start_dialog_review(_FakeStorage(_dialog(), _messages()), "d1")
    assert "error" in out and out["review_uuid"] == review_uuid
    assert store.get_review(conn, review_uuid)["status"] == "running"


def test_start_dialog_review_replaces_stale_after_dialog_threshold(conn, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from eval.review import store
    from server import review_service

    review_uuid = store.create_review(conn, "dialog", "d1", "v1", "m")
    created = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    conn.execute("UPDATE reviews SET created_at=? WHERE review_uuid=?", (created, review_uuid))
    conn.commit()
    monkeypatch.setattr(review_service, "get_trace_conn", lambda: conn)
    out = review_service.start_dialog_review(_FakeStorage(_dialog(), _messages()), "d1")
    assert out["status"] == "running" and out["review_uuid"] != review_uuid
    old = store.get_review(conn, review_uuid)
    assert old["status"] == "failed" and "stale" in old["error"]

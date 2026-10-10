"""TestClient 路由测试：/api/eval/reviews*（monkeypatch service，不触 LLM）。"""
import sqlite3

import pytest

from utils.db_schema.registry import ALL_DBS
from utils.db_schema.migrator import ensure_schema


@pytest.fixture()
def conn(tmp_path):
    # TestClient 在 worker 线程执行请求，连接需允许跨线程（与生产 _open_trace_conn 一致）
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    d = ALL_DBS["agent_trace"]
    ensure_schema(c, d.tables, d.special_migrations)
    yield c
    c.close()


class _Storage:
    def get_dialog(self, uuid):
        return {"dialog_uuid": uuid, "title": "t", "current_mode": "react_stock"} if uuid == "d1" else None

    def list_messages(self, uuid):
        return []

    def list_dialogs(self):
        return [{"dialog_uuid": "d1", "title": "t", "updated_at": "2026-09-18T10:00:00"}]


def _client(monkeypatch, storage, conn, tasks):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.routes import reviews as reviews_route

    app = FastAPI()
    app.include_router(reviews_route.router)

    class _Web:
        pass

    web = _Web()
    web.storage = storage
    # get_web_state 是普通函数（非 Depends），dependency_overrides 不生效，用 monkeypatch
    monkeypatch.setattr(reviews_route, "get_web_state", lambda request: web)
    monkeypatch.setattr(reviews_route.review_service, "get_trace_conn", lambda: conn)
    monkeypatch.setattr(reviews_route.review_service, "run_dialog_review_task",
                        lambda storage, uuid, rid: tasks.append((uuid, rid)))
    return TestClient(app)


def test_list_dialogs_endpoint(monkeypatch, conn):
    tasks = []
    c = _client(monkeypatch, _Storage(), conn, tasks)
    r = c.get("/api/eval/reviews/dialogs")
    assert r.status_code == 200
    assert r.json()["items"][0]["dialog_uuid"] == "d1"


def test_get_reviews_requires_dialog_uuid(monkeypatch, conn):
    c = _client(monkeypatch, _Storage(), conn, [])
    assert c.get("/api/eval/reviews").status_code == 400


def test_get_reviews_missing_dialog(monkeypatch, conn):
    c = _client(monkeypatch, _Storage(), conn, [])
    assert c.get("/api/eval/reviews", params={"dialog_uuid": "nope"}).status_code == 404


def test_post_review_starts_background_task(monkeypatch, conn):
    tasks = []
    c = _client(monkeypatch, _Storage(), conn, tasks)
    r = c.post("/api/eval/reviews", json={"dialog_uuid": "d1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert tasks and tasks[0][0] == "d1"


def test_post_review_missing_dialog(monkeypatch, conn):
    c = _client(monkeypatch, _Storage(), conn, [])
    r = c.post("/api/eval/reviews", json={"dialog_uuid": "nope"})
    assert "error" in r.json()

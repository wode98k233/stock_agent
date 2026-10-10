"""viewer 评审 API 的 payload 函数与真实本地 HTTP 路由行为（分库形态）。

布局：tmp/agent_trace.db 仅 reviews（主库）；tmp/agent_trace/agent_trace_202609.db
含 runs/steps/messages 月库（run r1）。store 路径经 monkeypatch 指向 tmp。
"""
import http.client
import json
import socket
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from utils.agent_trace import server
from utils.agent_trace.models import ensure_month_schema, ensure_reviews_schema
from utils.agent_trace.store import TraceStore, store


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agent_trace.db")  # 主库（reviews）
    month_dir = tmp_path / "agent_trace"
    month_dir.mkdir()
    month_db = month_dir / "agent_trace_202609.db"

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_reviews_schema(conn)
    conn.commit()

    mconn = sqlite3.connect(str(month_db), check_same_thread=False)
    mconn.row_factory = sqlite3.Row
    ensure_month_schema(mconn)
    mconn.execute(
        "INSERT INTO runs(id,agent_name,input,status,created_at) "
        "VALUES('r1','react_stock','{}','success','2026-09-18T00:00:00')"
    )
    mconn.commit()

    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: db_path)
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(month_dir))
    monkeypatch.setattr(server, "_resolved_db", db_path)
    yield conn, db_path
    conn.close()
    mconn.close()
    store.close_thread_conns()


@pytest.fixture()
def http_env(trace_env, monkeypatch):
    # 在测试进程内付掉一次性冷 import（含 langchain），否则首个 POST 会在
    # 单线程 HTTPServer 内同步冷加载，并行测试下超过客户端超时导致必挂
    from eval.review import engine, store  # noqa: F401

    monkeypatch.setattr(server, "_start_review_thread", lambda uuid, run_id: None)
    if server.DualStackHTTPServer.address_family == socket.AF_INET6:
        host = "::1"
    else:
        host = "127.0.0.1"
    httpd = server.DualStackHTTPServer((host, 0), server.TraceHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield host, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _request(host, port, method, path, *, body=None, content_type=None):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    client = http.client.HTTPConnection(host, port, timeout=30)
    try:
        client.request(method, path, body=body, headers=headers)
        resp = client.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        client.close()


def test_create_review_payload_creates_pending(trace_env, monkeypatch):
    conn, db_path = trace_env
    started = []
    monkeypatch.setattr(
        server, "_start_review_thread",
        lambda uuid, run_id: started.append((uuid, run_id)),
    )
    out = server._create_review_payload({"run_id": "r1"})
    assert out["status"] == "running"
    assert out["review_uuid"]
    assert started and started[0][1] == "r1"
    row = conn.execute(
        "SELECT status FROM reviews WHERE review_uuid=?", (out["review_uuid"],)
    ).fetchone()
    assert row["status"] == "running"


def test_create_review_payload_requires_run_id():
    out = server._create_review_payload({})
    assert "error" in out


def test_create_review_payload_rejects_non_dict():
    out = server._create_review_payload([1])
    assert out == {"error": "invalid payload"}


def test_create_review_payload_dedupes_running(trace_env, monkeypatch):
    conn, _ = trace_env
    started = []
    monkeypatch.setattr(
        server, "_start_review_thread",
        lambda uuid, run_id: started.append(uuid),
    )
    first = server._create_review_payload({"run_id": "r1"})
    second = server._create_review_payload({"run_id": "r1"})
    assert first["status"] == "running"
    assert second["error"] == "该 run 已有评审进行中"
    assert second["review_uuid"] == first["review_uuid"]
    assert started == [first["review_uuid"]]
    count = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
    assert count == 1


def test_create_review_payload_allows_new_after_done(trace_env, monkeypatch):
    conn, _ = trace_env
    from eval.review import store

    started = []
    monkeypatch.setattr(
        server, "_start_review_thread",
        lambda uuid, run_id: started.append(uuid),
    )
    first = server._create_review_payload({"run_id": "r1"})
    store.finish_review(
        conn, first["review_uuid"], overall=90, scores={"tool_usage": 90},
        findings=[], inputs={"run_id": "r1"}, duration_ms=1.0,
    )
    second = server._create_review_payload({"run_id": "r1"})
    assert second["status"] == "running"
    assert second["review_uuid"] != first["review_uuid"]
    rows = conn.execute(
        "SELECT status FROM reviews WHERE scope='run' AND target_id='r1' ORDER BY id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["done", "running"]
    assert started == [first["review_uuid"], second["review_uuid"]]


def test_create_review_payload_replaces_stale_running(trace_env, monkeypatch):
    conn, _ = trace_env
    from eval.review import store

    stale_uuid = store.create_review(conn, "run", "r1", "v1", "m")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    conn.execute(
        "UPDATE reviews SET created_at=? WHERE review_uuid=?", (old_ts, stale_uuid)
    )
    conn.commit()
    started = []
    monkeypatch.setattr(
        server, "_start_review_thread",
        lambda uuid, run_id: started.append(uuid),
    )
    out = server._create_review_payload({"run_id": "r1"})
    assert out["status"] == "running"
    assert out["review_uuid"] != stale_uuid
    old = conn.execute(
        "SELECT status, error FROM reviews WHERE review_uuid=?", (stale_uuid,)
    ).fetchone()
    assert old["status"] == "failed"
    assert "stale" in old["error"]
    count = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
    assert count == 2


def test_create_review_payload_invalid_created_at_stays_blocked(trace_env, monkeypatch):
    conn, _ = trace_env
    from eval.review import store

    existing_uuid = store.create_review(conn, "run", "r1", "v1", "m")
    conn.execute(
        "UPDATE reviews SET created_at='not-a-date' WHERE review_uuid=?", (existing_uuid,)
    )
    conn.commit()
    started = []
    monkeypatch.setattr(
        server, "_start_review_thread",
        lambda uuid, run_id: started.append(uuid),
    )
    out = server._create_review_payload({"run_id": "r1"})
    assert out["error"] == "该 run 已有评审进行中"
    assert out["review_uuid"] == existing_uuid
    assert started == []


def test_reviews_payload_lists_items(trace_env):
    conn, _ = trace_env
    conn.execute(
        "INSERT INTO reviews(review_uuid,scope,target_id,rubric_version,judge_model,"
        "status,overall,scores_json,findings_json,created_at) "
        "VALUES('u1','run','r1','v1','m','done',88,'{\"tool_usage\": 88}','[]','2026-09-18T00:00:01')"
    )
    conn.commit()
    out = server._reviews_payload("r1")
    assert out["items"][0]["review_uuid"] == "u1"
    assert out["items"][0]["overall"] == 88
    assert out["items"][0]["scores"] == {"tool_usage": 88}


def test_reviews_payload_empty_without_run_id(trace_env):
    assert server._reviews_payload("") == {"items": []}


def test_http_post_valid_json_creates_running(http_env, trace_env):
    conn, _ = trace_env
    status, raw = _request(
        *http_env, "POST", "/api/reviews",
        body=json.dumps({"run_id": "r1"}),
        content_type="application/json",
    )
    out = json.loads(raw)
    assert status == 200
    assert out["status"] == "running"
    assert out["review_uuid"]
    row = conn.execute(
        "SELECT status FROM reviews WHERE review_uuid=?", (out["review_uuid"],)
    ).fetchone()
    assert row["status"] == "running"


def test_http_post_non_dict_rejected(http_env):
    status, raw = _request(
        *http_env, "POST", "/api/reviews",
        body=json.dumps([1]),
        content_type="application/json",
    )
    assert status == 200
    assert json.loads(raw) == {"error": "invalid payload"}


def test_http_post_non_json_content_type_rejected(http_env):
    status, raw = _request(
        *http_env, "POST", "/api/reviews",
        body=json.dumps({"run_id": "r1"}),
        content_type="text/plain",
    )
    assert status == 200
    assert json.loads(raw) == {"error": "unsupported media type"}


def test_http_post_content_type_case_and_params_accepted(http_env):
    status, raw = _request(
        *http_env, "POST", "/api/reviews",
        body=json.dumps({"run_id": "r1"}),
        content_type="Application/JSON; charset=utf-8",
    )
    assert status == 200
    assert json.loads(raw)["status"] == "running"


def test_http_post_unknown_path_404(http_env):
    status, _ = _request(
        *http_env, "POST", "/api/nope",
        body="{}",
        content_type="application/json",
    )
    assert status == 404


def test_http_get_reviews_empty_run_id(http_env):
    status, raw = _request(*http_env, "GET", "/api/reviews")
    assert status == 200
    assert json.loads(raw) == {"items": []}


def test_review_worker_marks_failed_on_exception(trace_env, monkeypatch):
    conn, _ = trace_env
    from eval.review import engine, store

    review_uuid = store.create_review(conn, "run", "r1", "v1", "m")

    def boom(*args, **kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr(engine, "execute", boom)
    server._review_worker(review_uuid, "r1")
    row = conn.execute(
        "SELECT status, error FROM reviews WHERE review_uuid=?", (review_uuid,)
    ).fetchone()
    assert row["status"] == "failed"
    assert "worker 异常" in row["error"]


def test_start_review_thread_runs_worker(trace_env, monkeypatch):
    from eval.review import engine

    calls = []
    done = threading.Event()

    def fake_execute(conn, uuid, run_id, llm=None):
        calls.append((uuid, run_id))
        done.set()

    monkeypatch.setattr(engine, "execute", fake_execute)
    server._start_review_thread("u-smoke", "r1")
    assert done.wait(timeout=5)
    assert calls == [("u-smoke", "r1")]

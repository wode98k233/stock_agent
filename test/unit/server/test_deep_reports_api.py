"""长报告 API 单测：不启动完整 server，只挂路由 + 假 DB/service。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routes import deep_reports as route


class FakeDB:
    def __init__(self, events=None, report=None):
        self._events = events or []
        self._report = report

    def list_reports(self, limit=50):
        return [{"rid": "dr1", "title": "银行板块深度", "status": "done", "progress": 100,
                 "sections_done": 2, "sections_total": 2, "words": 1200, "universe": "银行板块"}]

    def events_since(self, rid, since_id=0, limit=500):
        return [e for e in self._events if e["id"] > since_id]

    def get_report(self, rid):
        return self._report


def _client():
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB(events=[
        {"id": 1, "type": "progress", "created_at": "", "data": {"progress": 50}},
        {"id": 2, "type": "done", "created_at": "", "data": {"status": "done", "words": 1200}},
    ], report={"rid": "dr1", "status": "done"})
    import agents.deep_report.db as dbmod
    monkeypatch.setattr(dbmod, "get_db", lambda: db)
    return db


class TestDeepReportAPI:
    def test_create_returns_rid(self, monkeypatch):
        import server.deep_report_service as svc
        monkeypatch.setattr(svc, "start", lambda **kw: "drtest")
        r = _client().post("/api/deep-reports", json={"universe": "银行板块"})
        assert r.status_code == 200
        assert r.json()["data"]["rid"] == "drtest"

    def test_create_with_dialog_writes_card(self, monkeypatch):
        import server.deep_report_service as svc
        import server.deps as deps
        monkeypatch.setattr(svc, "start", lambda **kw: "drcard")
        created = {}

        class FakeStorage:
            def create_message(self, **kw):
                created.update(kw)
                return {"message_uuid": "m1"}

        class FakeWeb:
            storage = FakeStorage()

        monkeypatch.setattr(deps, "get_web_state", lambda req: FakeWeb())
        r = _client().post("/api/deep-reports",
                           json={"universe": "银行板块", "dialog_uuid": "d1"})
        assert r.status_code == 200
        assert created["dialog_uuid"] == "d1" and created["role"] == "assistant"
        assert "@@DEEP_REPORT_START@@" in created["content"]
        assert '"rid": "drcard"' in created["content"]

    def test_create_without_dialog_writes_nothing(self, monkeypatch):
        import server.deep_report_service as svc
        import server.deps as deps
        monkeypatch.setattr(svc, "start", lambda **kw: "drnodialog")
        called = {"n": 0}

        class FakeStorage:
            def create_message(self, **kw):
                called["n"] += 1
                return {}

        class FakeWeb:
            storage = FakeStorage()

        monkeypatch.setattr(deps, "get_web_state", lambda req: FakeWeb())
        _client().post("/api/deep-reports", json={"universe": "银行板块"})
        assert called["n"] == 0

    def test_list(self, fake_db):
        r = _client().get("/api/deep-reports")
        assert r.status_code == 200
        assert r.json()["data"][0]["rid"] == "dr1"

    def test_get_404(self, monkeypatch):
        import server.deep_report_service as svc
        monkeypatch.setattr(svc, "snapshot", lambda rid: None)
        assert _client().get("/api/deep-reports/nope").status_code == 404

    def test_get_ok(self, monkeypatch):
        import server.deep_report_service as svc
        monkeypatch.setattr(svc, "snapshot", lambda rid: {"rid": rid, "status": "writing"})
        body = _client().get("/api/deep-reports/dr9").json()
        assert body["data"]["status"] == "writing"

    def test_events_replay_and_stop(self, fake_db):
        r = _client().get("/api/deep-reports/dr1/events?since_id=0")
        assert r.status_code == 200
        text = r.text
        assert "event: progress" in text
        assert "event: done" in text and '"words": 1200' in text

    def test_events_since_cursor(self, fake_db):
        r = _client().get("/api/deep-reports/dr1/events?since_id=1")
        assert "event: progress" not in r.text
        assert "event: done" in r.text

    def test_cancel(self, monkeypatch):
        import server.deep_report_service as svc
        monkeypatch.setattr(svc, "cancel", lambda rid: True)
        body = _client().post("/api/deep-reports/dr1/cancel").json()
        assert body["data"]["cancelled"] is True


    def test_create_rejects_unknown_template(self):
        r = _client().post("/api/deep-reports",
                           json={"universe": "银行板块", "template_id": "nope"})
        assert r.status_code == 400

    def test_bad_rid_rejected(self):
        assert _client().get("/api/deep-reports/abc.def").status_code == 400


def _client_with_db(monkeypatch, db):
    import agents.deep_report.db as dbmod
    monkeypatch.setattr(dbmod, "get_db", lambda: db)
    return _client()


class TestEventStreamLifecycle:
    """SSE 游标 / 未知 rid / 终态收流（评审缺陷 1、2、3）。"""

    def _events(self):
        return [
            {"id": 1, "type": "progress", "created_at": "", "data": {"progress": 50}},
            {"id": 2, "type": "done", "created_at": "", "data": {"status": "done", "words": 1200}},
        ]

    def test_last_event_id_header_wins_over_since_id(self, monkeypatch):
        # 浏览器 EventSource 重连时会带 Last-Event-ID 头；必须优先于 URL 上的 since_id。
        db = FakeDB(events=self._events(), report={"rid": "dr1", "status": "done"})
        client = _client_with_db(monkeypatch, db)

        r = client.get("/api/deep-reports/dr1/events?since_id=0",
                       headers={"Last-Event-ID": "1"})
        assert r.status_code == 200
        assert "event: progress" not in r.text      # 头游标 1 → 不再重放 0/1
        assert "event: done" in r.text

        # 反向：头游标 0 压过 since_id=1（证明读的是头，不是查询参数）
        r2 = client.get("/api/deep-reports/dr1/events?since_id=1",
                        headers={"Last-Event-ID": "0"})
        assert "event: progress" in r2.text

    def test_unknown_rid_terminates_instead_of_30min_heartbeat(self, monkeypatch):
        db = FakeDB(events=self._events(), report=None)
        client = _client_with_db(monkeypatch, db)
        t0 = time.monotonic()
        r = client.get("/api/deep-reports/dr404/events?since_id=0")
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert "event: error" in r.text and "报告不存在" in r.text
        assert "event: progress" not in r.text      # 不存在就不该重放任何事件
        assert elapsed < 5, f"未知 rid 未及时终止（{elapsed:.1f}s）"

    def test_terminal_report_ends_stream_after_events(self, monkeypatch):
        # 只有非终态事件 + 报告行已是 done：事件发完后必须收流（不傻等 30 分钟）
        db = FakeDB(events=self._events()[:1], report={"rid": "dr1", "status": "done"})
        client = _client_with_db(monkeypatch, db)
        t0 = time.monotonic()
        r = client.get("/api/deep-reports/dr1/events?since_id=0")
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert "event: progress" in r.text
        assert "event: heartbeat" not in r.text
        assert elapsed < 5, f"终态后事件流未及时结束（{elapsed:.1f}s）"
        assert "event: done" not in r.text


class TestEventLoopOffload:
    """P1-B 回归：同步 SQLite / snapshot 重试不能阻塞事件循环。"""

    @staticmethod
    def _request():
        from starlette.requests import Request
        return Request({
            "type": "http", "method": "GET", "path": "/api/deep-reports/dr1/events",
            "headers": [], "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "client": ("testclient", 123),
        })

    @staticmethod
    def _patch_to_thread(monkeypatch, calls):
        async def fake_to_thread(func, *args, **kwargs):
            calls.append(getattr(func, "__name__", str(func)))
            return func(*args, **kwargs)

        monkeypatch.setattr(route.asyncio, "to_thread", fake_to_thread)

    def test_stream_events_offloads_db_calls(self, monkeypatch):
        calls: list[str] = []
        db = FakeDB(
            events=[{"id": 1, "type": "done", "created_at": "",
                     "data": {"status": "done", "words": 1200}}],
            report={"rid": "dr1", "status": "done"},
        )
        import agents.deep_report.db as dbmod
        monkeypatch.setattr(dbmod, "get_db", lambda: db)
        self._patch_to_thread(monkeypatch, calls)

        async def consume():
            response = await route.stream_events("dr1", self._request(), since_id=0)
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(consume())
        assert any("event: done" in c for c in chunks)
        assert "get_report" in calls and "events_since" in calls, calls

    def test_get_report_offloads_snapshot(self, monkeypatch):
        calls: list[str] = []

        def fake_snapshot(rid):
            return {"rid": rid, "status": "writing"}

        import server.deep_report_service as svc
        monkeypatch.setattr(svc, "snapshot", fake_snapshot)
        self._patch_to_thread(monkeypatch, calls)

        body = asyncio.run(route.get_report("dr1"))
        assert body["data"]["status"] == "writing"
        assert "fake_snapshot" in calls, calls

    def test_idle_stream_emits_heartbeat(self, monkeypatch):
        # 长任务无事件时必须有帧（首拍心跳）；否则代理/浏览器可能判定空流断开
        db = FakeDB(events=[], report={"rid": "dr1", "status": "writing"})
        import agents.deep_report.db as dbmod
        monkeypatch.setattr(dbmod, "get_db", lambda: db)

        async def first_chunk():
            response = await route.stream_events("dr1", self._request(), since_id=0)
            agen = response.body_iterator
            chunk = await agen.__anext__()
            await agen.aclose()
            return chunk

        chunk = asyncio.run(first_chunk())
        assert "event: heartbeat" in chunk



    def test_rejects_oversized_inputs(self):
        assert _client().post("/api/deep-reports",
                              json={"universe": "银行" * 40}).status_code == 422
        assert _client().post("/api/deep-reports",
                              json={"universe": "银行板块",
                                    "dialog_uuid": "../etc"}).status_code == 422


    def test_plan_extracts_universe(self, monkeypatch):
        import utils.llm_factory as lf

        class _Fake:
            def invoke(self, messages):
                return type("R", (), {"content": '{"universe": "银行板块", '
                                                 '"template_id": "deep_sector_finance", '
                                                 '"request": "银行板块能追吗"}'})()

        monkeypatch.setattr(lf, "get_report_llm", lambda: _Fake())
        r = _client().post("/api/deep-reports/plan",
                           json={"text": "银行板块现在还能追高吗？给我深度报告"})
        d = r.json()["data"]
        assert d["universe"] == "银行板块" and d["fallback"] is False
        assert d["template_id"] == "deep_sector_finance"

    def test_plan_falls_back_when_llm_fails(self, monkeypatch):
        import utils.llm_factory as lf

        def boom():
            raise RuntimeError("llm down")

        monkeypatch.setattr(lf, "get_report_llm", boom)
        text = "银行板块现在还能追高吗？给我深度报告"
        r = _client().post("/api/deep-reports/plan", json={"text": text})
        d = r.json()["data"]
        assert d["fallback"] is True
        assert d["universe"] == text[:20]


    def test_templates_endpoint(self):
        r = _client().get("/api/deep-reports/templates")
        assert r.status_code == 200
        ids = [x["id"] for x in r.json()["data"]]
        assert len(ids) >= 9 and "deep_sector_baijiu" in ids
        assert any(x["name"] for x in r.json()["data"])

    def test_markdown_is_attachment(self, tmp_path, monkeypatch):
        import server.routes.deep_reports as mod

        class _S:
            def dir_of(self, rid):
                return tmp_path

        monkeypatch.setattr(mod, "_store", lambda: _S())
        (tmp_path / "report.md").write_text("# 标题\n正文", encoding="utf-8")
        r = _client().get("/api/deep-reports/drOK1/markdown")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        assert "标题" in r.text

    def test_pdf_404_when_html_missing(self, tmp_path, monkeypatch):
        import server.routes.deep_reports as mod

        class _S:
            def dir_of(self, rid):
                return tmp_path

        monkeypatch.setattr(mod, "_store", lambda: _S())
        assert _client().get("/api/deep-reports/drOK2/pdf").status_code == 404

    def test_pdf_returns_pdf_bytes(self, tmp_path, monkeypatch):
        import server.routes.deep_reports as mod

        class _S:
            def dir_of(self, rid):
                return tmp_path

        monkeypatch.setattr(mod, "_store", lambda: _S())
        monkeypatch.setattr(mod, "_render_pdf", lambda url, rid: b"%PDF-1.4 fake")
        (tmp_path / "report.html").write_text("<html><body>x</body></html>", encoding="utf-8")
        r = _client().get("/api/deep-reports/drOK3/pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert "attachment" in r.headers.get("content-disposition", "")

"""overview API 测试：快照形状 / 开关 / SSE 帧。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routes import overview as ov


def _client(monkeypatch, runner=None):
    app = FastAPI()
    app.include_router(ov.router)
    monkeypatch.setattr(ov, "get_web_state", lambda request: None)
    if runner is not None:
        monkeypatch.setattr(ov, "run_stream", runner)
    return TestClient(app)


def test_snapshot_disabled(monkeypatch):
    monkeypatch.setattr(ov, "_enabled", lambda: False)
    r = _client(monkeypatch).get("/api/overview/snapshot")
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False


def test_snapshot_shape(monkeypatch):
    monkeypatch.setattr(ov, "_enabled", lambda: True)
    monkeypatch.setattr(ov, "load_snapshot", lambda ctx, blocks=None, force=False: {
        "blocks": {"runtime": {"ok": True, "cache": False, "stale_cache": False,
                               "degraded": [], "ms": 5, "data": {"available": True}}},
        "generated_at": "t"})
    r = _client(monkeypatch).get("/api/overview/snapshot", params={"blocks": "runtime"})
    body = r.json()
    assert body["code"] == 0 and body["data"]["enabled"] is True
    assert body["data"]["blocks"]["runtime"]["data"]["available"] is True


def test_stream_emits_plan_and_final(monkeypatch):
    monkeypatch.setattr(ov, "_enabled", lambda: True)

    def fake_run(ctx, blocks=None, force=False, emit=None):
        emit({"event": "plan", "data": {"blocks": [], "force": force, "ts": "t"}})
        emit({"event": "final", "data": {"total": 0, "ok": 0, "failed": 0,
                                         "elapsed_ms": 1, "degraded": [], "ts": "t"}})

    with _client(monkeypatch, runner=fake_run).stream("GET", "/api/overview/stream") as r:
        text = "".join(chunk for chunk in r.iter_text())
    assert "event: plan" in text and "event: final" in text and '"force": false' in text

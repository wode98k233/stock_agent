"""公司事件服务/API/日历 payload（不联网、不碰真实库）。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_list_events_adds_type_label(monkeypatch):
    import server.events_service as svc

    monkeypatch.setattr(svc, "query_corporate_events", lambda **kw: [
        {"event_id": "a", "code": "600519", "event_type": "unlock",
         "event_date": "2026-11-09", "title": "T", "payload": None}])
    rows = svc.list_events(date_from="2026-11-01", date_to="2026-11-30")
    assert rows[0]["type_label"] == "解禁"


def test_group_by_date():
    import server.events_service as svc

    rows = [{"event_date": "2026-11-09", "event_id": "a"},
            {"event_date": "2026-11-09", "event_id": "b"},
            {"event_date": "2026-11-10", "event_id": "c"}]
    grouped = svc.group_by_date(rows)
    assert len(grouped["2026-11-09"]) == 2 and len(grouped["2026-11-10"]) == 1


def test_events_api_returns_envelope(monkeypatch):
    import server.routes.events as mod

    monkeypatch.setattr(mod, "list_events", lambda **kw: [
        {"event_id": "a", "code": "600519", "event_type": "unlock",
         "event_date": "2026-11-09", "title": "T", "payload": None}])
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)
    r = client.get("/api/events", params={"codes": "600519", "from": "2026-11-01",
                                          "to": "2026-11-30"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert len(body["data"]["events"]) == 1


def test_calendar_payload_includes_events(monkeypatch):
    import server.calendar_service as cs
    import server.events_service as svc

    monkeypatch.setattr(cs, "load_trading_days", lambda y, m: ["2026-11-09"])
    monkeypatch.setattr(cs, "load_dialog_days", lambda s, y, m: {})
    monkeypatch.setattr(cs, "load_trace_token_stats", lambda s, d: {})
    monkeypatch.setattr(cs, "load_deep_report_days", lambda y, m: {})
    monkeypatch.setattr(svc, "load_corporate_events",
                        lambda y, m: {"2026-11-09": [{"title": "x", "event_type": "unlock"}]})
    payload = cs.build_calendar_payload(object(), 2026, 11)
    assert "2026-11-09" in payload["events"]
    assert {t["key"] for t in payload["event_types"]} >= {"unlock", "dividend"}

"""盯盘告警集成测试：TestClient HTTP 层全链路 + 手动 poll 编排。

行情通道（新浪批量报价）的真实可用性已由 P1-4 实测冒烟验证，
此处 mock 行情聚焦「API → store → scheduler → notifier → 落库」链路。
"""
from unittest.mock import MagicMock

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


@pytest.fixture(scope="module")
def client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("需要 fastapi[testclient]")
    from unittest.mock import AsyncMock
    from server.app import create_app
    # 路由在 lifespan 阶段注册，必须以 context manager 进入触发
    state = MagicMock()
    state.runtime.shutdown = AsyncMock()
    with TestClient(create_app(state=state)) as c:
        yield c


@pytest.fixture
def store(tmp_path, monkeypatch):
    from server.monitoring.store import MonitorStore
    from server.routes import market_monitor as mm
    s = MonitorStore(str(tmp_path / "m.db"))
    monkeypatch.setattr(mm, "_get_store", lambda: s)
    return s


class TestMonitorHTTPAPI:
    """HTTP 层 CRUD 全链路（走完整 FastAPI 栈）。"""

    def test_rules_crud_via_http(self, client, store):
        # 创建
        resp = client.post("/api/monitor/rules", json={
            "stock_code": "600519", "stock_name": "贵州茅台",
            "rule_type": "price_below", "threshold": 1300.0})
        assert resp.status_code == 200
        rule_id = resp.json()["id"]

        # 列表
        resp = client.get("/api/monitor/rules")
        assert resp.status_code == 200
        assert any(r["id"] == rule_id for r in resp.json()["items"])

        # 停用
        resp = client.patch(f"/api/monitor/rules/{rule_id}",
                            json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 删除
        resp = client.delete(f"/api/monitor/rules/{rule_id}")
        assert resp.status_code == 200
        assert client.get("/api/monitor/rules").json()["items"] == []

    def test_add_rule_invalid_type_400(self, client, store):
        resp = client.post("/api/monitor/rules", json={
            "stock_code": "600519", "rule_type": "macd_golden", "threshold": 1.0})
        assert resp.status_code == 400

    def test_alerts_read_flow_via_http(self, client, store):
        id1 = store.record_alert(1, "600519", "贵州茅台", "a")
        store.record_alert(1, "600519", "贵州茅台", "b")

        resp = client.get("/api/monitor/alerts?unread=true")
        assert resp.json()["total"] == 2

        resp = client.post("/api/monitor/alerts/read", json={"ids": [id1]})
        assert resp.json()["updated"] == 1

        resp = client.get("/api/monitor/alerts?unread=true")
        assert resp.json()["total"] == 1

        # 全部已读
        resp = client.post("/api/monitor/alerts/read", json={"ids": [], "all": True})
        assert resp.json()["updated"] == 1

    def test_status_endpoint(self, client, store):
        resp = client.get("/api/monitor/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False  # 测试环境未挂载 scheduler
        assert "interval_s" in data
        assert "notification_enabled" in data

    def test_test_notification_endpoint(self, client, store, monkeypatch):
        from server.routes import market_monitor as mm
        monkeypatch.setattr(
            mm, "send_notification",
            lambda *a, **kw: [MagicMock(success=True, channel="test")])
        resp = client.post("/api/monitor/test")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert "test" in resp.json()["channels_ok"]


class TestFullPollCycle:
    """全链路：规则 → 手动 poll 两轮（穿越）→ 推送 + 落库 + 一次性停用。"""

    def test_full_cycle(self, store):
        from server.monitoring.scheduler import MonitorScheduler

        notifier = MagicMock()
        notifier.push.return_value = "test-channel"
        quotes = MagicMock()
        scheduler = MonitorScheduler(
            storage=MagicMock(), store=store, quotes=quotes, notifier=notifier)
        scheduler._storage.watchlist.list_stocks.return_value = [
            {"stock_code": "600519", "stock_name": "贵州茅台", "market": "cn"}]

        # 建规则：价格上穿 1500，一次性
        store.add_rule("600519", "贵州茅台", "price_above", 1500.0, once_only=True)

        # 第一轮：基线（1490，未越线）
        quotes.fetch.return_value = {
            "600519": {"code": "600519", "name": "贵州茅台", "price": 1490.0, "pct": 0.5}}
        r1 = scheduler.poll_once()
        assert r1["alerts"] == 0

        # 第二轮：上穿 1505 → 触发
        quotes.fetch.return_value = {
            "600519": {"code": "600519", "name": "贵州茅台", "price": 1505.0, "pct": 0.8}}
        r2 = scheduler.poll_once()
        assert r2["alerts"] == 1

        # 推送已发、告警已落库、一次性规则已停用
        notifier.push.assert_called_once()
        alerts = store.list_alerts()["items"]
        assert len(alerts) == 1
        assert alerts[0]["rule_desc"] == "贵州茅台 价格上穿 1500.0"
        assert alerts[0]["channels_ok"] == "test-channel"
        assert store.list_rules(enabled_only=True) == []

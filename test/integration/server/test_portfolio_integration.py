"""持仓账本集成测试：TestClient HTTP 层全链路（lifespan 注册路由）。

行情通道 mock（新浪批量接口真实可用性已由盯盘方向实测），聚焦
「HTTP API → PortfolioStorage → 事务重算」链路。
"""
from unittest.mock import AsyncMock, MagicMock

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
    from server.app import create_app
    state = MagicMock()
    state.runtime.shutdown = AsyncMock()
    with TestClient(create_app(state=state)) as c:
        yield c


@pytest.fixture
def portfolio(client, tmp_path, monkeypatch):
    """用临时 WebStorage 替换路由的 _get_portfolio（模块级注入点）。"""
    from server.storage import WebStorage
    from server.routes import portfolio as pf
    storage = WebStorage(tmp_path / "web.db")
    monkeypatch.setattr(pf, "_get_portfolio", lambda req=None: storage.portfolio)
    # 画像持仓同步会写真实 data/user_profile/default.json（UPM 固定用户 default），
    # 测试中禁用以避免把测试持仓写进生产用户画像。
    monkeypatch.setattr(pf, "_sync_profile_holdings", lambda _p: None)
    return storage.portfolio


class TestPortfolioHTTP:
    def test_positions_empty_via_http(self, client, portfolio):
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_full_trade_flow_via_http(self, client, portfolio, monkeypatch):
        from server.routes import portfolio as pf
        monkeypatch.setattr(pf, "_fetch_quotes_sync", lambda codes, source="sina": {
            "600519": {"code": "600519", "price": 1500.0, "pct": 1.5}})

        # 记买入
        resp = client.post("/api/portfolio/trade", json={
            "stock_code": "600519", "stock_name": "贵州茅台", "side": "buy",
            "qty": 100, "price": 1300.0, "trade_date": "2026-09-01"})
        assert resp.status_code == 200
        buy_uuid = resp.json()["trade_uuid"]

        # 持仓 + 行情合并
        resp = client.get("/api/portfolio/positions")
        data = resp.json()
        item = data["items"][0]
        assert item["price"] == 1500.0
        assert item["float_pnl"] == pytest.approx(20000.0)
        assert data["summary"]["total_market_value"] == pytest.approx(150000.0)

        # 卖出一半
        resp = client.post("/api/portfolio/trade", json={
            "stock_code": "600519", "side": "sell", "qty": 50,
            "price": 1450.0, "trade_date": "2026-09-02"})
        assert resp.status_code == 200
        sell_uuid = resp.json()["trade_uuid"]

        # 删买入流水：剩余卖出将超持仓 → 400 拒绝、状态不变
        resp = client.delete(f"/api/portfolio/trade/{buy_uuid}")
        assert resp.status_code == 400
        assert client.get("/api/portfolio/trades").json()["total"] == 2

        # 删卖出流水：合法 → 持仓恢复 100 股、已实现盈亏回退
        resp = client.delete(f"/api/portfolio/trade/{sell_uuid}")
        assert resp.status_code == 200
        data = client.get("/api/portfolio/positions").json()
        assert data["items"][0]["qty"] == 100
        assert data["items"][0]["realized_pnl"] == pytest.approx(0)

    def test_sell_exceeds_returns_400(self, client, portfolio):
        client.post("/api/portfolio/trade", json={
            "stock_code": "600519", "side": "buy", "qty": 100,
            "price": 1300.0, "trade_date": "2026-09-01"})
        resp = client.post("/api/portfolio/trade", json={
            "stock_code": "600519", "side": "sell", "qty": 999,
            "price": 1400.0, "trade_date": "2026-09-02"})
        assert resp.status_code == 400
        assert "持仓不足" in resp.json()["detail"]

    def test_invalid_code_returns_400(self, client, portfolio):
        resp = client.post("/api/portfolio/trade", json={
            "stock_code": "12345", "side": "buy", "qty": 100,
            "price": 1300.0, "trade_date": "2026-09-01"})
        assert resp.status_code == 400

    def test_from_signal_via_http(self, client, portfolio, monkeypatch):
        from server.routes import portfolio as pf
        monkeypatch.setattr(pf, "_load_signal_row", lambda sid: {
            "id": sid, "decision": "buy", "symbol_key": "sh600519",
            "symbol_name": "贵州茅台", "dialog_uuid": "dlg_1",
            "signal_time": "2026-09-01 10:30:00"})

        resp = client.post("/api/portfolio/from-signal", json={
            "signal_id": 7, "qty": 100, "price": 1300.0})
        assert resp.status_code == 200

        # 幂等：二次记入 409
        resp = client.post("/api/portfolio/from-signal", json={
            "signal_id": 7, "qty": 100, "price": 1300.0})
        assert resp.status_code == 409

        trades = client.get("/api/portfolio/trades").json()["items"]
        assert trades[0]["source"] == "signal_eval"
        assert trades[0]["signal_id"] == 7

    def test_position_context_endpoint(self, client, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01",
                            stock_name="贵州茅台")
        resp = client.post("/api/portfolio/position-context")
        assert resp.status_code == 200
        assert "[用户持仓账本]" in resp.json()["text"]

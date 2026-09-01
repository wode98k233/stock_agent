"""图表路由测试。"""
import pandas as pd
from fastapi.testclient import TestClient

from server.app import create_app


def test_get_kline_reads_local_data_without_sync(monkeypatch):
    """读取 K 线只查本地缓存，不隐式同步外部数据。"""
    import server.routes.charts as charts

    df = pd.DataFrame([
        {
            'code': '600519',
            'trade_date': '2024-01-02',
            'open': 10.0,
            'high': 11.0,
            'low': 9.5,
            'close': 10.5,
            'volume': 1000,
            'amount': 10000,
            'turnover_rate': 1.2,
            'pct_change': 2.0,
        }
    ])
    monkeypatch.setattr(charts, "_sync_kline_sync", lambda code, days: (_ for _ in ()).throw(AssertionError("不应隐式同步")))
    monkeypatch.setattr("utils.cache.market_data_db.get_stock_daily", lambda *args, **kwargs: df)
    monkeypatch.setattr("utils.cache.market_data_db.get_stock_info", lambda code: {'name': '贵州茅台'})

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/chart/kline/600519?days=30&adjust=none")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "600519"
    assert body["name"] == "贵州茅台"
    assert body["kline"][0]["date"] == "2024-01-02"


def test_get_kline_returns_no_local_data_without_sync(monkeypatch):
    """无本地数据时返回可识别错误，不自动拉取。"""
    import server.routes.charts as charts

    monkeypatch.setattr(charts, "_sync_kline_sync", lambda code, days: (_ for _ in ()).throw(AssertionError("不应隐式同步")))
    monkeypatch.setattr("utils.cache.market_data_db.get_stock_daily", lambda *args, **kwargs: pd.DataFrame())

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/chart/kline/600519?days=30")

    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "no_local_data"


def test_sync_kline_calls_explicit_sync(monkeypatch):
    """同步接口显式触发数据采集。"""
    import server.routes.charts as charts

    called = {}

    def fake_sync(code, days, source="auto"):
        called["code"] = code
        called["days"] = days
        called["source"] = source
        return 3

    monkeypatch.setattr(charts, "_sync_kline_sync", fake_sync)

    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/chart/kline/sync", json={"code": "600519", "days": 250, "source": "akshare"})

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "code": "600519", "count": 3, "source": "akshare"}
    assert called == {"code": "600519", "days": 250, "source": "akshare"}

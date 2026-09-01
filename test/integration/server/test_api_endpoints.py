"""Web API 端点集成测试

使用 FastAPI TestClient 测试关键 API 端点。
不需要启动真实服务器。
"""
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


@pytest.fixture(scope="module")
def client():
    """创建 FastAPI TestClient"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("需要 fastapi[testclient]")

    from server.app import create_app
    app = create_app()
    return TestClient(app)


# ── /api/modes ──────────────────────────────────────────────

class TestModesAPI:
    """模式列表 API"""

    def test_get_modes(self, client):
        """GET /api/modes 返回 200"""
        resp = client.get("/api/modes")
        assert resp.status_code == 200

    def test_modes_has_list(self, client):
        """返回 modes 列表"""
        resp = client.get("/api/modes")
        data = resp.json()
        assert "modes" in data
        assert isinstance(data["modes"], list)

    def test_modes_have_required_fields(self, client):
        """每个 mode 有 name/label"""
        resp = client.get("/api/modes")
        for mode in resp.json()["modes"]:
            assert "name" in mode
            assert "label" in mode


# ── /api/datasources ────────────────────────────────────────

class TestDatasourcesAPI:
    """数据源 API"""

    def test_get_datasources(self, client):
        """GET /api/datasources 返回 200"""
        resp = client.get("/api/datasources")
        assert resp.status_code == 200


# ── /api/data/stats ─────────────────────────────────────────

class TestDataStatsAPI:
    """数据统计 API"""

    def test_get_stats(self, client):
        """GET /api/data/stats 返回 200"""
        resp = client.get("/api/data/stats")
        assert resp.status_code == 200

    def test_stats_has_tables(self, client):
        """返回统计信息"""
        resp = client.get("/api/data/stats")
        data = resp.json()
        assert isinstance(data, dict)


# ── /api/strategies ─────────────────────────────────────────

class TestStrategiesAPI:
    """策略列表 API"""

    def test_get_strategies(self, client):
        """GET /api/backtest/strategies 返回 200"""
        resp = client.get("/api/backtest/strategies")
        assert resp.status_code == 200

    def test_strategies_list(self, client):
        """返回策略列表"""
        resp = client.get("/api/backtest/strategies")
        data = resp.json()
        assert isinstance(data, (list, dict))


# ── /api/watchlist ──────────────────────────────────────────

class TestWatchlistAPI:
    """自选股 API"""

    def test_get_watchlist(self, client):
        """GET /api/watchlist 返回 200"""
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200


# ── /api/report-templates ───────────────────────────────────

class TestReportTemplatesAPI:
    """报告模板 API"""

    def test_get_templates(self, client):
        """GET /api/report-templates 返回 200"""
        resp = client.get("/api/report-templates")
        assert resp.status_code == 200


# ── /api/dialogs ────────────────────────────────────────────

class TestDialogsAPI:
    """对话 API"""

    def test_list_dialogs(self, client):
        """GET /api/dialogs 返回 200"""
        resp = client.get("/api/dialogs")
        assert resp.status_code == 200

    def test_create_dialog(self, client):
        """POST /api/dialogs 创建对话"""
        resp = client.post("/api/dialogs", json={
            "title": "集成测试对话",
            "mode": "react_stock",
        })
        assert resp.status_code in (200, 201)


# ── 404 ─────────────────────────────────────────────────────

class TestNotFound:
    """不存在的端点"""

    def test_unknown_endpoint(self, client):
        """不存在的端点返回 404"""
        resp = client.get("/api/nonexistent_endpoint_999")
        assert resp.status_code == 404

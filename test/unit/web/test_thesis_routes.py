"""研究台账路由测试：HTTP 层形状、路径优先级、错误不 500。

服务层整体替换，不发网络请求、不碰真实库。重点验证两件事：
1. `/list` 与 `/alerts` 不能被 `/{thesis_id}` 吞掉（FastAPI 按声明顺序匹配）
2. 任何异常都要转成 `{code:1, msg}` 而不是 500 —— 台账页白屏比报错更难查
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from server.routes import thesis as routes
    from server import thesis_service as svc
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), svc, monkeypatch


class TestListAndAlerts:
    def test_list_returns_items(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "list_theses", lambda: [
            {"thesis_id": "dividend_env", "name": "红利环境因子包", "has_snapshot": True}])
        r = tc.get("/api/thesis/list")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        assert body["data"]["count"] == 1
        assert body["data"]["items"][0]["thesis_id"] == "dividend_env"

    def test_list_not_shadowed_by_path_param(self, client):
        """`/list` 若被 /{thesis_id} 抢走，这里会变成 detail 的形状。"""
        tc, svc, mp = client
        mp.setattr(svc, "list_theses", lambda: [])
        mp.setattr(svc, "get_detail", lambda tid: pytest.fail("不该走 detail"))
        body = tc.get("/api/thesis/list").json()
        assert "items" in body["data"]

    def test_cross_thesis_alerts(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_alerts", lambda tid, days: [
            {"thesis_id": "a", "severity": "warn", "message": "m"}])
        body = tc.get("/api/thesis/alerts?days=7").json()
        assert body["code"] == 0
        assert body["data"]["count"] == 1

    def test_list_failure_is_degraded_not_500(self, client):
        tc, svc, mp = client

        def boom():
            raise RuntimeError("库锁住了")

        mp.setattr(svc, "list_theses", boom)
        r = tc.get("/api/thesis/list")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 1
        assert "库锁住了" in body["msg"]


class TestDetail:
    def test_detail_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_detail", lambda tid: {
            "thesis_id": tid, "name": "n", "definition": {"id": tid},
            "snapshot": {"total": 4}, "alerts": [], "alert_summary": {"total": 0},
            "definition_ok": True, "definition_problems": [], "definition_history": []})
        body = tc.get("/api/thesis/dividend_env").json()
        assert body["code"] == 0
        assert body["data"]["snapshot"]["total"] == 4

    def test_missing_definition_returns_error_code(self, client):
        """定义不存在是配置问题，必须明确报错，不能返回空详情让人以为是没数据。"""
        tc, svc, mp = client
        mp.setattr(svc, "get_detail", lambda tid: {
            "thesis_id": tid, "definition": {}, "snapshot": None,
            "definition_ok": False, "definition_problems": ["不存在"]})
        body = tc.get("/api/thesis/nope").json()
        assert body["code"] == 1
        assert "不存在" in body["msg"]

    def test_detail_exception_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_detail", lambda tid: (_ for _ in ()).throw(ValueError("坏定义")))
        body = tc.get("/api/thesis/x").json()
        assert body["code"] == 1


class TestRefresh:
    def test_refresh_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "refresh", lambda tid: {
            "snapshot": {"total": 4, "trade_date": "2026-09-17"},
            "alerts": [], "new_alert_count": 0,
            "definition_changed": False, "definition_problems": [],
            "snapshot_created": True})
        body = tc.post("/api/thesis/dividend_env/refresh").json()
        assert body["code"] == 0
        assert body["data"]["snapshot_created"] is True

    def test_refresh_value_error_is_friendly(self, client):
        """定义不可用必须原样透出原因（这是配置问题，用户能自己修）。"""
        tc, svc, mp = client

        def boom(tid):
            raise ValueError("台账定义不可用：[x] 阈值 a 未覆盖低端")

        mp.setattr(svc, "refresh", boom)
        body = tc.post("/api/thesis/broken/refresh").json()
        assert body["code"] == 1
        assert "未覆盖低端" in body["msg"]

    def test_refresh_unexpected_error_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "refresh", lambda tid: (_ for _ in ()).throw(RuntimeError("接口挂了")))
        body = tc.post("/api/thesis/x/refresh").json()
        assert body["code"] == 1
        assert "接口挂了" in body["msg"]


class TestHistoryAndAlerts:
    def test_history(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_history", lambda tid, days: {
            "thesis_id": tid, "items": [{"trade_date": "2026-09-17", "total": 4}], "count": 1})
        body = tc.get("/api/thesis/dividend_env/history?days=30").json()
        assert body["code"] == 0
        assert body["data"]["count"] == 1

    def test_per_thesis_alerts(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_alerts", lambda tid, days: [])
        body = tc.get("/api/thesis/dividend_env/alerts").json()
        assert body["code"] == 0
        assert body["data"]["items"] == []

    def test_history_failure_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_history", lambda tid, days: (_ for _ in ()).throw(RuntimeError("x")))
        body = tc.get("/api/thesis/a/history").json()
        assert body["code"] == 1


class TestRouteTable:
    def test_declaration_order_keeps_static_paths_first(self):
        """回归：静态路径必须先于 /{thesis_id} 声明，否则会被路径参数吞掉。

        /portfolio 是 2026-09-22 新增的组合建议端点——漏了这条，请求
        /api/thesis/portfolio 会变成「台账定义不存在: portfolio」。
        """
        from server.routes.thesis import router
        paths = [r.path for r in router.routes]
        idx = paths.index("/api/thesis/{thesis_id}")
        for p in ("/api/thesis/list", "/api/thesis/alerts", "/api/thesis/portfolio",
                  "/api/thesis/market-matrix"):
            assert paths.index(p) < idx, p

    def test_route_module_registered_in_app(self):
        import inspect
        from server import app as app_mod
        src = inspect.getsource(app_mod.register_routes)
        assert '"thesis"' in src

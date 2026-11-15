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


class TestReport:
    """报告数据接口（契约 5.4）：一次组装详情 + 历史 + 证据 + 归因 + 告警。"""

    def test_report_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_report_data", lambda tid: {
            "generated_at": "2026-10-27 20:30",
            "thesis": {"id": tid, "name": "红利环境因子包"},
            "snapshot": {"total": 4},
            "history": {"count": 2, "items": []},
            "attribution": {"total_delta": 1, "rows": [],
                            "note": "总分 +3 → +4（+1），其中 ④ +1；其余不变"},
            "evidence": {"ok": True, "rows": []},
            "alerts": [],
        })
        body = tc.get("/api/thesis/dividend_env/report").json()
        assert body["code"] == 0
        assert body["data"]["attribution"]["total_delta"] == 1
        assert body["data"]["evidence"]["ok"] is True
        assert body["data"]["generated_at"] == "2026-10-27 20:30"

    def test_report_force_flag_passed_through(self, client):
        """`?force=1` 必须透传给服务层，否则前端「重新生成」会拿到 30 分钟缓存。"""
        tc, svc, mp = client
        seen = {}

        def fake(tid, **kw):
            seen.update(kw)
            return {"generated_at": "x", "alerts": []}

        mp.setattr(svc, "get_report_data", fake)
        body = tc.get("/api/thesis/dividend_env/report?force=1").json()
        assert body["code"] == 0 and seen.get("force") is True

    def test_report_not_shadowed_by_detail(self, client):
        """`/report` 若被 /{thesis_id} 抢走，这里会变成 detail 的形状。"""
        tc, svc, mp = client
        mp.setattr(svc, "get_detail", lambda tid: pytest.fail("不该走 detail"))
        mp.setattr(svc, "get_report_data", lambda tid: {"generated_at": "x", "alerts": []})
        body = tc.get("/api/thesis/dividend_env/report").json()
        assert body["code"] == 0 and "generated_at" in body["data"]

    def test_missing_definition_returns_error_code(self, client):
        tc, svc, mp = client

        def boom(tid):
            raise ValueError("台账定义不存在: nope")

        mp.setattr(svc, "get_report_data", boom)
        body = tc.get("/api/thesis/nope/report").json()
        assert body["code"] == 1
        assert "不存在" in body["msg"]

    def test_unexpected_error_degraded_not_500(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_report_data",
                   lambda tid: (_ for _ in ()).throw(RuntimeError("组装挂了")))
        r = tc.get("/api/thesis/x/report")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 1 and "组装挂了" in body["msg"]


class TestFactorEvidenceDetails:
    def test_details_flag_passed_through(self, client):
        """报告要 details=1（滚动曲线 + 分层明细），普通汇总不能带（体积大）。"""
        tc, svc, mp = client
        seen = {}

        def fake(tid, details=False):
            seen["details"] = details
            return {"ok": True, "rows": []}

        mp.setattr(svc, "get_all_factor_evidence", fake)
        body = tc.get("/api/thesis/dividend_env/factor-evidence?details=1").json()
        assert body["code"] == 0 and seen["details"] is True
        tc.get("/api/thesis/dividend_env/factor-evidence")
        assert seen["details"] is False

    def test_details_failure_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_all_factor_evidence",
                   lambda tid, details=False: (_ for _ in ()).throw(RuntimeError("x")))
        body = tc.get("/api/thesis/a/factor-evidence?details=1").json()
        assert body["code"] == 1


class TestChart:
    """双轴复盘图接口：形状 + 失败降级（不 500）+ force/points 透传。"""

    PAYLOAD = {
        "thesis_id": "bond_fund", "name": "债券基金环境因子包",
        "def_version": "1.3", "def_hash": "5708b764",
        "target": {"code": "H11001", "name": "中证全债",
                   "freq": "daily", "freq_label": "日频"},
        "h": 20, "n": 3, "points": 3, "thin": 1,
        "range": ["2026-10-28", "2026-10-30"],
        "generated_at": "2026-11-01 22:30:00", "cached": False,
        "metrics": {"r_level": 0.57, "r_diff": 0.14,
                    "r_fwd": -0.02, "r_fwd_flip": 0.12},
        "signs": {"coupon": -1}, "dropped": ["gov_supply"],
        "dates": ["2026-10-28", "2026-10-29", "2026-10-30"],
        "total": [1.0, 2.0, 3.0], "flip": [1.0, 2.0, 3.0],
        "price": [1.0, 1.1, 1.2], "fut": [1.1, 1.2, None],
    }

    def test_chart_ok_shape(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_chart_data",
                   lambda tid, force=False, points=1200: dict(self.PAYLOAD))
        body = tc.get("/api/thesis/bond_fund/chart").json()
        assert body["code"] == 0
        d = body["data"]
        # 前端契约字段：一个都不能少
        for key in ("thesis_id", "name", "def_version", "def_hash", "target", "h",
                    "n", "range", "generated_at", "cached", "metrics", "signs",
                    "dropped", "points", "thin", "dates", "total", "flip",
                    "price", "fut"):
            assert key in d, key
        assert d["def_hash"] == "5708b764"
        assert d["target"]["code"] == "H11001"
        assert d["target"]["freq_label"] == "日频"
        assert d["cached"] is False
        # 四个数组必须与 dates 等长，fut 尾部为 None
        assert len({len(d[k]) for k in ("dates", "total", "flip", "price", "fut")}) == 1
        assert d["fut"][-1] is None

    def test_force_and_points_passed_through(self, client):
        """`?force=1` 绕过缓存、`points` 控制抽稀——漏传会让前端「重新生成」失效。"""
        tc, svc, mp = client
        seen = {}

        def fake(tid, force=False, points=1200):
            seen.update({"tid": tid, "force": force, "points": points})
            return dict(self.PAYLOAD)

        mp.setattr(svc, "get_chart_data", fake)
        body = tc.get("/api/thesis/bond_fund/chart?force=1&points=800").json()
        assert body["code"] == 0
        assert seen == {"tid": "bond_fund", "force": True, "points": 800}

    def test_chart_failure_degraded_not_500(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_chart_data",
                   lambda tid, force=False, points=1200: (_ for _ in ()).throw(
                       RuntimeError("取数挂了")))
        r = tc.get("/api/thesis/bond_fund/chart")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 1
        assert body["data"] is None
        assert "取数挂了" in body["msg"]

    def test_missing_definition_returns_error_code(self, client):
        tc, svc, mp = client

        def boom(tid, force=False, points=1200):
            raise ValueError("台账定义不可用：定义文件损坏")

        mp.setattr(svc, "get_chart_data", boom)
        body = tc.get("/api/thesis/nope/chart").json()
        assert body["code"] == 1
        assert "定义不可用" in body["msg"]


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
        # /report、/chart 子路径不冲突（段数不同），但按契约声明在 /{thesis_id} 之前
        assert paths.index("/api/thesis/{thesis_id}/report") < idx
        assert paths.index("/api/thesis/{thesis_id}/chart") < idx

    def test_route_module_registered_in_app(self):
        import inspect
        from server import app as app_mod
        src = inspect.getsource(app_mod.register_routes)
        assert '"thesis"' in src

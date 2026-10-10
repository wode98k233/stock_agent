"""/api/crowding 路由测试：HTTP 形状、降级不 500、参数校验、触发转发到后台巡检。

服务层整体替换，不发网络请求、不碰真实库。重点：
1. 空库/读失败要返回 `{code:1, msg}` 或 `available:False`，不能 500 —— 监控页白屏最难查
2. 扫描触发只做**转发**（`/api/scans/run/crowding_scan`），不在路由里另跑一遍全量补数
"""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from server.routes import crowding as routes
    from server import crowding_service as svc
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), svc, monkeypatch


def _latest(**over):
    base = {
        "available": True, "trade_date": "2026-09-17", "scanned_at": "2026-09-17 21:00:00",
        "pool_size": 516, "computed_count": 3, "insufficient_count": 495,
        "stale_count": 18, "hot_count": 0, "collected_count": 12,
        "levels": [[40, "舒适"], [60, "中性"], [101, "过热"]],
        "labels": {"ext": "均线延伸"},
        "levels_order": ["舒适", "中性", "过热"],
        "counts_by_level": {"舒适": 1, "中性": 1, "过热": 1},
        "pool_problems": [], "warnings": [],
        "items": [{"key": "index:000300", "name": "沪深300", "score": 55.0, "status": "ok"}],
        "item_total": 1,
    }
    base.update(over)
    return base


class TestLatest:
    def test_latest_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_latest", lambda limit: _latest())
        body = tc.get("/api/crowding/latest").json()
        assert body["code"] == 0
        assert body["data"]["pool_size"] == 516
        assert body["data"]["items"][0]["key"] == "index:000300"

    def test_limit_is_forwarded(self, client):
        tc, svc, mp = client
        seen = {}
        mp.setattr(svc, "get_latest", lambda limit: (seen.update(limit=limit), _latest())[1])
        tc.get("/api/crowding/latest?limit=50")
        assert seen["limit"] == 50

    def test_limit_out_of_range_is_422(self, client):
        tc, _svc, _mp = client
        assert tc.get("/api/crowding/latest?limit=0").status_code == 422
        assert tc.get("/api/crowding/latest?limit=99999").status_code == 422

    def test_latest_failure_is_degraded_not_500(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_latest",
                   lambda limit: (_ for _ in ()).throw(RuntimeError("库锁住了")))
        r = tc.get("/api/crowding/latest")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 1
        assert "库锁住了" in body["msg"]

    def test_empty_store_shape_is_passthrough(self, client):
        """没跑过扫描时服务层返回 available=False，路由要原样透出而不是当错误。"""
        tc, svc, mp = client
        mp.setattr(svc, "get_latest", lambda limit: {
            "available": False, "items": [], "levels": [], "labels": {},
            "counts_by_level": {}, "pool_problems": ["池配置不存在"]})
        body = tc.get("/api/crowding/latest").json()
        assert body["code"] == 0
        assert body["data"]["available"] is False
        assert body["data"]["pool_problems"] == ["池配置不存在"]


class TestHistoryAndItem:
    def test_history_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_history", lambda days: {
            "items": [{"trade_date": "2026-09-17", "hot_count": 4}], "count": 1})
        body = tc.get("/api/crowding/history?days=30").json()
        assert body["code"] == 0
        assert body["data"]["count"] == 1

    def test_history_failure_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_history",
                   lambda days: (_ for _ in ()).throw(RuntimeError("x")))
        body = tc.get("/api/crowding/history").json()
        assert body["code"] == 1

    def test_item_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_item_history", lambda key, limit: {
            "item_key": key, "items": [{"trade_date": "2026-09-17", "score": 62.0}],
            "count": 1})
        body = tc.get("/api/crowding/item?key=index:000300").json()
        assert body["code"] == 0
        assert body["data"]["item_key"] == "index:000300"

    def test_item_requires_key(self, client):
        tc, _svc, _mp = client
        assert tc.get("/api/crowding/item").status_code == 422
        assert tc.get("/api/crowding/item?key=").status_code == 422

    def test_item_failure_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_item_history",
                   lambda key, limit: (_ for _ in ()).throw(RuntimeError("坏了")))
        body = tc.get("/api/crowding/item?key=index:A").json()
        assert body["code"] == 1
        assert "坏了" in body["msg"]


class TestPool:
    def test_pool_ok(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_pool_info", lambda: {
            "path": "data/crowding/pool.json",
            "pool": {"params": {"ma_days": 250}}, "problems": []})
        body = tc.get("/api/crowding/pool").json()
        assert body["code"] == 0
        assert body["data"]["pool"]["params"]["ma_days"] == 250
        assert body["data"]["problems"] == []

    def test_pool_failure_degraded(self, client):
        tc, svc, mp = client
        mp.setattr(svc, "get_pool_info",
                   lambda: (_ for _ in ()).throw(RuntimeError("配置读不了")))
        body = tc.get("/api/crowding/pool").json()
        assert body["code"] == 1
        assert "配置读不了" in body["msg"]


class TestTriggerScan:
    def _wire_manager(self, monkeypatch, fn=None, freeze_register=True):
        """装一个全新的 manager 顶掉进程单例，返回 (mgr, calls)。"""
        from server.background_scans import ScanJob, ScanManager
        import server.background_scans as bs
        import server.scan_jobs as sj

        mgr = ScanManager()
        calls = []

        def default_fn():
            calls.append(1)
            return {"summary": "池 516 · 算分 3"}

        mgr.register(ScanJob(key="crowding_scan", name="全市场拥挤度扫描",
                             fn=fn or default_fn))
        monkeypatch.setattr(bs, "get_manager", lambda: mgr)
        if freeze_register:
            monkeypatch.setattr(sj, "register_default_jobs",
                                lambda manager=None: ["crowding_scan"])
        return mgr, calls

    def test_trigger_returns_accepted_immediately(self, client, monkeypatch):
        tc, _svc, mp = client
        self._wire_manager(mp)
        body = tc.post("/api/crowding/scan").json()
        assert body["code"] == 0
        assert body["data"]["accepted"] is True
        assert "后台" in body["data"]["msg"]

    async def test_trigger_is_not_blocked_by_a_slow_job(self, monkeypatch):
        """首轮要补几百个板块（上游 2–5s 限流，十几分钟）——请求必须立刻返回。

        注意不能用 TestClient 量这个：它每个请求起一个 blocking portal，退出时会等
        事件循环排空，于是「后台任务」看起来像同步的。这里用 ASGITransport，
        让请求跑在一个持续活着的循环上，才测得出真实行为。
        """
        entered, done, release = threading.Event(), threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(5)
            done.set()
            return {"summary": "跑完了"}

        mgr, _calls = self._wire_manager(monkeypatch, fn=slow)
        app = FastAPI()
        from server.routes import crowding as routes
        app.include_router(routes.router)

        transport = httpx.ASGITransport(app=app)
        t0 = time.time()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/crowding/scan")
            elapsed = time.time() - t0
            assert r.json()["data"]["accepted"] is True
            assert elapsed < 1.0, "触发被同步等待了"
            for _ in range(300):        # 让出事件循环，等后台 task 起来
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set(), "作业没转后台"
            assert mgr.is_running("crowding_scan") is True
            assert not done.is_set(), "请求返回时作业就已经跑完了？"
        release.set()

    def test_running_crowding_scan_is_not_triggered_twice(self, client, monkeypatch):
        """单飞：手动点两次不会让几百个板块的补数跑两遍。"""
        tc, _svc, mp = client
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(5)
            return {"summary": "跑完了"}

        mgr, calls = self._wire_manager(mp, fn=slow)
        t = threading.Thread(target=mgr.run, args=("crowding_scan",), daemon=True)
        t.start()
        assert entered.wait(3)

        body = tc.post("/api/crowding/scan").json()
        assert body["code"] == 0
        assert body["data"]["accepted"] is False
        assert body["data"]["running"] is True
        assert "正在进行中" in body["data"]["msg"]
        assert calls == []          # 慢作业没被第二次跑

        release.set()
        t.join(5)

    def test_trigger_falls_back_to_register_default_jobs(self, client, monkeypatch):
        """未经 bootstrap（manager 里没有该作业）时应自动补注册而不是报未知作业。"""
        tc, _svc, mp = client
        from server.background_scans import ScanJob, ScanManager
        import server.background_scans as bs
        import server.scan_jobs as sj

        mgr = ScanManager()
        mp.setattr(bs, "get_manager", lambda: mgr)

        def _register(manager=None):
            target = manager if manager is not None else mgr
            target.register(ScanJob(key="crowding_scan", name="拥挤度",
                                    fn=lambda: {"summary": "ok"}))
            return ["crowding_scan"]

        mp.setattr(sj, "register_default_jobs", _register)
        body = tc.post("/api/crowding/scan").json()
        assert body["code"] == 0
        assert body["data"]["accepted"] is True

    def test_trigger_failure_is_degraded(self, client, monkeypatch):
        tc, _svc, mp = client
        import server.background_scans as bs
        mp.setattr(bs, "get_manager",
                   lambda: (_ for _ in ()).throw(RuntimeError("调度器不可用")))
        body = tc.post("/api/crowding/scan").json()
        assert body["code"] == 1
        assert "调度器不可用" in body["msg"]


class TestRouteTable:
    def test_paths_declared(self):
        from server.routes.crowding import router
        paths = {r.path for r in router.routes}
        assert paths == {"/api/crowding/latest", "/api/crowding/history",
                         "/api/crowding/item", "/api/crowding/pool",
                         "/api/crowding/scan"}

    def test_route_module_registered_in_app(self):
        import inspect
        from server import app as app_mod
        src = inspect.getsource(app_mod.register_routes)
        assert '"crowding"' in src

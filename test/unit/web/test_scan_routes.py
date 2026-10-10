"""/api/scans 路由测试：状态读取、触发即返回、同步等待、单飞拦截。

**绝不碰 `get_manager()` 的进程单例** —— 每个用例都用全新的 ScanManager 顶掉。
否则会污染同进程其它测试：`/api/data-monitor/scan` 的默认路径会从「同步回退」
变成「转后台」，那边的断言随之漂移（同一 worker 串状态造成的假失败很难查）。
"""
import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_app():
    from server.routes import scans as routes
    app = FastAPI()
    app.include_router(routes.router)
    return app


@pytest.fixture
def env(monkeypatch):
    from server.background_scans import ScanJob, ScanManager
    import server.background_scans as bs
    import server.scan_jobs as sj

    mgr = ScanManager()
    monkeypatch.setattr(bs, "get_manager", lambda: mgr)

    def _register(manager=None):
        target = manager if manager is not None else mgr
        jobs = [
            ScanJob(key="storage_scan", name="存储监控扫描",
                    fn=lambda: {"summary": "扫了 3 个资产", "assets": 3}),
            ScanJob(key="crowding_scan", name="全市场拥挤度扫描",
                    fn=lambda: {"summary": "池 516", "pool_size": 516}),
        ]
        for job in jobs:
            target.register(job)
        return [j.key for j in jobs]

    monkeypatch.setattr(sj, "register_default_jobs", _register)
    return TestClient(_build_app()), mgr, monkeypatch


class TestStatus:
    def test_status_lists_jobs(self, env):
        tc, _mgr, _mp = env
        body = tc.get("/api/scans/status").json()
        assert body["code"] == 0
        keys = [j["key"] for j in body["data"]["jobs"]]
        assert keys == ["storage_scan", "crowding_scan"]
        job = body["data"]["jobs"][0]
        assert job["running"] is False and job["run_count"] == 0
        assert job["last_ok"] is None

    def test_status_autoregisters_when_manager_empty(self, env):
        """没经过 bootstrap（如单测直接挂路由）时 /status 不能是空列表。"""
        tc, mgr, _mp = env
        assert mgr.keys() == []
        tc.get("/api/scans/status")
        assert mgr.keys() == ["storage_scan", "crowding_scan"]

    def test_status_failure_is_degraded_not_500(self, env):
        tc, _mgr, mp = env

        def boom(manager=None):
            raise RuntimeError("注册表坏了")

        import server.scan_jobs as sj
        mp.setattr(sj, "register_default_jobs", boom)
        r = tc.get("/api/scans/status")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 1
        assert "注册表坏了" in body["msg"]


class TestRunSync:
    def test_wait_returns_result(self, env):
        tc, _mgr, _mp = env
        body = tc.post("/api/scans/run/storage_scan?wait=1").json()
        assert body["code"] == 0
        assert body["data"]["ok"] is True
        assert body["data"]["summary"] == "扫了 3 个资产"
        assert body["data"]["metrics"]["assets"] == 3

    def test_wait_surfaces_unknown_key(self, env):
        tc, _mgr, _mp = env
        body = tc.post("/api/scans/run/nope?wait=1").json()
        assert body["code"] == 1
        assert "未知巡检作业" in body["msg"]

    def test_wait_unknown_key_does_not_run_anything(self, env):
        tc, mgr, _mp = env
        tc.post("/api/scans/run/nope?wait=1")
        assert all(j["run_count"] == 0 for j in mgr.status()["jobs"])

    def test_wait_on_failing_job_is_degraded(self, env):
        tc, mgr, mp = env
        from server.background_scans import ScanJob
        mgr.register(ScanJob(key="bad", name="坏的",
                             fn=lambda: (_ for _ in ()).throw(RuntimeError("接口挂了"))))
        body = tc.post("/api/scans/run/bad?wait=1").json()
        assert body["code"] == 0                  # 执行类失败仍以状态形式返回
        assert body["data"]["ok"] is False
        assert "接口挂了" in body["data"]["error"]


class TestRunBackground:
    def test_default_trigger_returns_immediately(self, env):
        tc, _mgr, _mp = env
        body = tc.post("/api/scans/run/crowding_scan").json()
        assert body["code"] == 0
        assert body["data"]["accepted"] is True
        assert body["data"]["key"] == "crowding_scan"

    def test_running_job_is_not_triggered_twice(self, env):
        tc, mgr, _mp = env
        from server.background_scans import ScanJob
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(5)
            return {"summary": "跑完了"}

        mgr.register(ScanJob(key="storage_scan2", name="慢", fn=slow))
        t = threading.Thread(target=mgr.run, args=("storage_scan2",), daemon=True)
        t.start()
        assert entered.wait(3)

        body = tc.post("/api/scans/run/storage_scan2").json()
        assert body["code"] == 0
        assert body["data"]["accepted"] is False
        assert body["data"]["running"] is True
        assert "未重复触发" in body["data"]["msg"]

        release.set()
        t.join(5)

    async def test_background_trigger_really_runs_on_the_loop(self, env):
        """「触发即返回」不能变成「触发了但没跑」—— 用 ASGITransport 让事件循环活着。"""
        _tc, mgr, _mp = env
        transport = httpx.ASGITransport(app=_build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post("/api/scans/run/storage_scan")
            assert r.json()["data"]["accepted"] is True
            # 轮询条件必须等到「跑完」而不是「开始跑」：run() 在**开头**就 +1 run_count，
            # running 却到 finally 才清掉。只看 run_count 会撞进「已开跑但未结束」的中间态，
            # 随后的断言就成了竞态（本机实测已 flaky 一次）。
            for _ in range(200):
                cur = mgr.status()["jobs"][0]
                if cur["run_count"] >= 1 and not cur["running"]:
                    break
                await asyncio.sleep(0.01)
        st = mgr.status()["jobs"][0]
        assert st["run_count"] == 1
        assert st["last_ok"] is True
        assert st["last_source"] == "manual"
        assert st["running"] is False


class TestRouteTable:
    def test_paths_declared(self):
        from server.routes.scans import router
        paths = {r.path for r in router.routes}
        assert paths == {"/api/scans/status", "/api/scans/run/{key}"}

    def test_route_module_registered_in_app(self):
        import inspect
        from server import app as app_mod
        src = inspect.getsource(app_mod.register_routes)
        assert '"scans"' in src

"""WarmupGateMiddleware 启动导入门闸测试

2026-10-01 web 冻结修复：预热线程 import 期间，handler 函数体懒加载会在
事件循环线程撞上 import 模块锁 → 整站冻结（route-preview 143993ms 实锤）。
门闸让请求异步等待（循环保持响应），health/static 豁免，失败兜底放行。
"""
import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from utils import bootstrap_common
from utils.bootstrap_common import (
    _arm_import_gate,
    _mark_agent_imports_done,
    _mark_skill_imports_done,
    _reset_import_gate_for_tests,
    imports_ready,
)
from server.middleware import WarmupGateMiddleware


@pytest.fixture(autouse=True)
def _clean_gate():
    """每个用例前后恢复门闸初始状态，防止污染其他测试"""
    _reset_import_gate_for_tests()
    yield
    _reset_import_gate_for_tests()


class TestGateState:
    """门闸状态机"""

    def test_default_ready_when_not_armed(self):
        """未 armed（CLI / 未起预热）默认放行"""
        assert imports_ready() is True

    def test_armed_blocks_until_both_phases_done(self):
        _arm_import_gate()
        assert imports_ready() is False

        _mark_agent_imports_done()
        assert imports_ready() is False  # skill 阶段未完成

        _mark_skill_imports_done()
        assert imports_ready() is True

    def test_skill_done_alone_not_enough(self):
        _arm_import_gate()
        _mark_skill_imports_done()
        assert imports_ready() is False

    def test_marks_are_idempotent(self):
        _arm_import_gate()
        for _ in range(3):
            _mark_agent_imports_done()
            _mark_skill_imports_done()
        assert imports_ready() is True


def _build_app() -> FastAPI:
    """构造带门闸中间件的最小 app（与 server/app.py 相同的装配方式）"""
    app = FastAPI()
    app.add_middleware(WarmupGateMiddleware)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/eval/runs")          # 名单内（GATED_PREFIXES）
    async def gated_api():
        return {"ok": True}

    @app.get("/api/dialogs")            # 名单外数据接口 → 即时放行
    async def dialogs():
        return {"dialogs": []}

    @app.get("/api/modes")              # 已 to_thread 化，名单外 → 即时放行
    async def modes():
        return {"modes": []}

    @app.get("/api/datasources")        # 曾被 /api/data 前缀误伤，须放行
    async def datasources():
        return {"datasources": []}

    @app.get("/api/backtest/strategies")  # backtest 树无预热竞态，已移出名单
    async def strategies():
        return {"strategies": []}

    @app.get("/api/dialogs/d1/group-messages")  # 名单内（GATED_SUFFIXES）
    async def group_messages():
        return {"ok": True}

    @app.get("/static/x.js")
    async def static_file():
        return {"ok": True}

    @app.get("/")
    async def index():
        return {"page": "shell"}

    return app


class TestGateMatching:
    """名单匹配规则"""

    def test_gated_paths(self):
        for p in ("/api/eval/runs", "/api/chart/fragments",
                  "/api/memory/stats", "/api/market-dashboard/snapshot",
                  "/api/traces/runs", "/api/reviews",
                  "/api/dialogs/d1/group-messages"):
            assert WarmupGateMiddleware._is_gated(p), p

    def test_ungated_paths(self):
        for p in ("/", "/static/x.js", "/api/health", "/api/dialogs",
                  "/api/watchlist", "/api/short-line/overview", "/api/thesis",
                  "/api/crowding/overview", "/api/overview/snapshot",
                  "/api/modes", "/api/datasources",          # to_thread 化后移出名单
                  "/api/backtest/strategies",                # backtest 树无预热竞态
                  "/api/agents",                             # to_thread 化后移出名单
                  "/api/report-templates/route-preview",     # to_thread 化后移出名单
                  "/api/config", "/api/tasks",
                  "/api/portfolio", "/api/news", "/api/skills"):
            assert not WarmupGateMiddleware._is_gated(p), p


class TestWarmupGateMiddleware:
    """中间件行为（httpx ASGITransport 走真异步）"""

    @pytest.mark.asyncio
    async def test_gated_request_pends_until_gate_opens(self):
        """名单内接口：门闸关闭时异步等待，放行后完成；循环期间 health 不受阻"""
        _arm_import_gate()
        app = _build_app()

        async def _open_gate_later():
            await asyncio.sleep(0.5)
            _mark_agent_imports_done()
            _mark_skill_imports_done()

        opener = asyncio.create_task(_open_gate_later())
        t0 = time.monotonic()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # 等待期间 health（豁免）应立即返回
            r_health = await c.get("/api/health")
            assert r_health.status_code == 200

            r = await c.get("/api/eval/runs")
        elapsed = time.monotonic() - t0
        await opener

        assert r.status_code == 200
        assert elapsed >= 0.4, f"名单内请求应等待门闸放行，实际 {elapsed:.2f}s"
        assert r_health.elapsed.total_seconds() < 0.4, "health 不应等待门闸"

    @pytest.mark.asyncio
    async def test_ungated_and_page_shell_pass_while_gate_closed(self):
        """名单外数据接口、页面壳、静态资源在门闸关闭期间立即放行"""
        _arm_import_gate()
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            t0 = time.monotonic()
            r1 = await c.get("/api/dialogs")
            r2 = await c.get("/static/x.js")
            r3 = await c.get("/")
            elapsed = time.monotonic() - t0
        assert r1.status_code == 200 and r1.json() == {"dialogs": []}
        assert r2.status_code == 200
        assert r3.status_code == 200 and r3.json() == {"page": "shell"}
        assert elapsed < 1.0, "名单外路径不应等待门闸"

    @pytest.mark.asyncio
    async def test_exact_and_suffix_rules(self):
        """GATED_SUFFIXES 命中挂起，而同前缀的普通路径与已 to_thread 化接口放行"""
        _arm_import_gate()
        app = _build_app()

        async def _open():
            await asyncio.sleep(0.4)
            _mark_agent_imports_done()
            _mark_skill_imports_done()

        opener = asyncio.create_task(_open())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            t0 = time.monotonic()
            r_gm = await c.get("/api/dialogs/d1/group-messages")   # suffix 命中 → 等待
            t_gm = time.monotonic() - t0
            t1 = time.monotonic()
            r_d = await c.get("/api/dialogs")                      # 普通 dialogs → 不等
            t_d = time.monotonic() - t1
            t2 = time.monotonic()
            r_ds = await c.get("/api/datasources")                 # 误伤回归：datasources 不等
            t_ds = time.monotonic() - t2
        await opener
        assert r_gm.status_code == 200 and t_gm >= 0.3
        assert r_d.status_code == 200 and t_d < 0.3
        assert r_ds.status_code == 200 and t_ds < 0.3

    @pytest.mark.asyncio
    async def test_open_gate_passes_immediately(self):
        """门闸放行后名单内请求直接通过（无等待）"""
        _arm_import_gate()
        _mark_agent_imports_done()
        _mark_skill_imports_done()
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            t0 = time.monotonic()
            r = await c.get("/api/eval/runs")
            elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_not_armed_passes_immediately(self):
        """未 armed（CLI/测试场景）一切照旧"""
        assert imports_ready() is True
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/eval/runs")
        assert r.status_code == 200


class TestGatedInflightCap:
    """预热期「挂起并发上限」：宁可甩 503，也不占满浏览器 6 连接

    2026-10-01 现象：一次首屏有 6 个 /api/chart + /api/market-dashboard 请求同时
    挂起 23~26s，占满同域连接池 → /api/dialogs（后端本就即时放行）被**浏览器排队**，
    用户体感「新建对话一直转圈」。故给门闸加并发上限：超出的请求立即 503 +
    Retry-After，前端 fetchJSON 退避重试。
    """

    @pytest.mark.asyncio
    async def test_exceeding_cap_sheds_503_fast(self, monkeypatch):
        """挂起数达上限 → 后续请求立即 503（带 Retry-After），不等门闸"""
        monkeypatch.setattr(WarmupGateMiddleware, "MAX_GATED_INFLIGHT", 1)
        _arm_import_gate()
        app = _build_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async def _open():
                await asyncio.sleep(0.8)
                _mark_agent_imports_done()
                _mark_skill_imports_done()

            opener = asyncio.create_task(_open())
            holding = asyncio.create_task(c.get("/api/eval/runs"))   # 占满唯一槽位
            await asyncio.sleep(0.25)                                # 确保它已进入等待

            t0 = time.monotonic()
            shed1 = await c.get("/api/eval/runs")
            t_shed = time.monotonic() - t0
            await opener
            waited = await holding

        assert shed1.status_code == 503
        assert shed1.headers.get("Retry-After"), "必须给出 Retry-After 供前端退避"
        assert shed1.json()["detail"]["retry_after"] >= 1
        assert t_shed < 0.4, f"甩挂必须即时（不占连接），实际 {t_shed:.2f}s"
        # 占用槽位的那条仍按原语义在门闸放行后成功
        assert waited.status_code == 200

    @pytest.mark.asyncio
    async def test_shed_does_not_affect_navigation_endpoints(self, monkeypatch):
        """甩挂期间，导航类接口（dialogs/页面壳）不受影响、始终可用"""
        monkeypatch.setattr(WarmupGateMiddleware, "MAX_GATED_INFLIGHT", 1)
        _arm_import_gate()
        app = _build_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async def _open():
                await asyncio.sleep(0.6)
                _mark_agent_imports_done()
                _mark_skill_imports_done()

            opener = asyncio.create_task(_open())
            holding = asyncio.create_task(c.get("/api/eval/runs"))
            await asyncio.sleep(0.2)

            t0 = time.monotonic()
            dialogs = await c.get("/api/dialogs")
            shell = await c.get("/")
            t_nav = time.monotonic() - t0
            await opener
            await holding

        assert dialogs.status_code == 200 and dialogs.json() == {"dialogs": []}
        assert shell.status_code == 200
        assert t_nav < 0.4, f"导航接口不应被门闸或甩挂拖累，实际 {t_nav:.2f}s"

    @pytest.mark.asyncio
    async def test_cap_not_applied_after_gate_open(self, monkeypatch):
        """门闸放行后不再甩挂（上限只作用于预热期）"""
        monkeypatch.setattr(WarmupGateMiddleware, "MAX_GATED_INFLIGHT", 1)
        _arm_import_gate()
        _mark_agent_imports_done()
        _mark_skill_imports_done()
        app = _build_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            rs = await asyncio.gather(
                c.get("/api/eval/runs"), c.get("/api/eval/runs"), c.get("/api/eval/runs"))
        assert all(r.status_code == 200 for r in rs)

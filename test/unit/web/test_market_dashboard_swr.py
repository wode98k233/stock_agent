"""市场面板快照的 stale-while-revalidate 与轮询分档（2026-10-18）。

事故：面板每 10 秒轮询，一旦命中某层 TTL 过期，请求路径上就做全市场拉取
（实测单次 8.5 秒），占着专属 worker 不放；同时页面上还有策略研究/K 线的重活，
整站互相拖慢到 5~12 秒。用户原话：「这个情绪面板老是卡住整个 web」。

这两条必须锁住：
  1. 有缓存 → 立刻返回（请求路径永不等待网络），续期丢后台且单飞；
  2. refresh_s 分档（交易 10s / 非交易 300s），否则收盘后请求量是必要的 30 倍。
"""
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest  # noqa: E402

from server import market_dashboard_service as svc  # noqa: E402
from server.routes import market_dashboard as md  # noqa: E402


@pytest.fixture()
def clean_cache(monkeypatch):
    monkeypatch.setattr(md, "_last_snapshot", None)
    monkeypatch.setattr(md, "_last_at", 0.0)
    monkeypatch.setattr(md, "_snapshot_refreshing", False)
    return md


class TestRefreshTier:

    def test_trading_uses_configured_interval(self, monkeypatch):
        monkeypatch.setattr(svc, "_is_trading_time", lambda: True)
        cfg = svc._config()
        assert cfg["refresh_s"] <= 60, "交易时段应保持高频轮询"

    def test_closed_uses_long_interval(self, monkeypatch):
        monkeypatch.setattr(svc, "_is_trading_time", lambda: False)
        assert svc._config()["refresh_s"] == svc._REFRESH_S_CLOSED
        assert svc._REFRESH_S_CLOSED >= 300

    def test_config_only_is_readonly_view(self, monkeypatch):
        monkeypatch.setattr(svc, "_is_trading_time", lambda: False)
        assert svc.build_config_only()["refresh_s"] == svc._REFRESH_S_CLOSED

    def test_cached_ttl_follows_refresh_s(self, monkeypatch):
        monkeypatch.setattr(svc, "_is_trading_time", lambda: False)
        assert md._cached_ttl() == float(svc._REFRESH_S_CLOSED)


def _fake_snapshot(marker="CACHED"):
    return {"enabled": True, "refresh_s": 300, "sentiment": None, "alerts": [],
            "breadth": None, "degraded": [], "stale": False, "trading": False,
            "marker": marker}


class TestStaleWhileRevalidate:

    def test_fresh_cache_returns_without_building(self, clean_cache, monkeypatch):
        md._last_snapshot = _fake_snapshot()
        md._last_at = time.time()

        def boom(*a, **k):
            raise AssertionError("有新鲜缓存时不得触达构建")
        monkeypatch.setattr(md, "_build", boom)

        data = __import__("asyncio").run(md.snapshot(None))["data"]
        assert data["marker"] == "CACHED"

    def test_stale_cache_returns_immediately_and_kicks_refresh(self, clean_cache, monkeypatch):
        md._last_snapshot = _fake_snapshot()
        md._last_at = time.time() - 100000      # 早已过期

        called = []
        monkeypatch.setattr(md, "_kick_refresh", lambda req: called.append(1))

        t0 = time.time()
        data = __import__("asyncio").run(md.snapshot(None))["data"]
        el = time.time() - t0
        assert data["marker"] == "CACHED", "过期也要先把旧数据给用户"
        assert el < 0.5, "过期缓存不得阻塞请求（实测旧实现会等 8.5s）"
        assert called, "过期必须触发后台续期"

    def test_refresh_is_single_flight(self, monkeypatch):
        monkeypatch.setattr(md, "_snapshot_refreshing", False)
        made = []
        monkeypatch.setattr(md._snapshot_executor, "submit",
                            lambda fn: made.append(fn))
        md._kick_refresh(None)
        md._kick_refresh(None)
        assert len(made) == 1, "同一时刻最多一个续期任务"
        assert md._snapshot_refreshing is True

    def test_store_snapshot_updates_time(self, clean_cache):
        md._store_snapshot(_fake_snapshot("X"))
        assert md._last_snapshot["marker"] == "X"
        assert md._last_at > 0

    def test_warm_snapshot_primes_cache(self, clean_cache, monkeypatch):
        """预热必须写进路由缓存，否则用户第一次打开仍要同步构建。"""
        monkeypatch.setattr(md, "_build", lambda req=None: _fake_snapshot("WARM"))
        md.warm_snapshot()
        assert md._last_snapshot["marker"] == "WARM"

    def test_warm_snapshot_skips_error_payload(self, clean_cache, monkeypatch):
        monkeypatch.setattr(md, "_build", lambda req=None: {"error": "boom"})
        md.warm_snapshot()
        assert md._last_snapshot is None, "失败结果不得进缓存"

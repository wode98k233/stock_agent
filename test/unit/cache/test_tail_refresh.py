"""读取侧尾部补齐 refresh_stale_tail 单测。

场景：stock_daily 缓存尾部陈旧（如 600900 停在 09-23），策略扫描/图表读取时
应对比「最近已完成交易日」，落后才增量拉取；新鲜不拉、冷却内不重复拉、
失败静默返回 False。
"""
from datetime import date

import pytest

from utils.cache import market_data_db as mdb


@pytest.fixture(autouse=True)
def clear_cooldown():
    mdb._tail_refresh_last.clear()
    yield
    mdb._tail_refresh_last.clear()


@pytest.fixture
def effective_today(monkeypatch):
    monkeypatch.setattr(
        "utils.trading_calendar.get_effective_trading_date",
        lambda market: date(2026, 9, 30),
    )


def _patch_latest(monkeypatch, value):
    monkeypatch.setattr(mdb, "get_latest_trade_date", lambda code: value)


class TestRefreshStaleTail:
    def test_stale_pulls_gap_and_returns_true(self, effective_today, monkeypatch):
        """落后于最近已完成交易日 → 拉缺口（≤ 缺口+余量，且至少 40 天），返回 True。"""
        _patch_latest(monkeypatch, "2026-09-23")
        captured = {}
        monkeypatch.setattr(
            mdb, "fetch_and_store_daily",
            lambda code, days, source="auto": captured.update(code=code, days=days) or 100,
        )
        assert mdb.refresh_stale_tail("600900") is True
        assert captured["code"] == "600900"
        assert captured["days"] >= 40

    def test_fresh_skips_fetch(self, effective_today, monkeypatch):
        """本地最新 = 最近已完成交易日 → 不拉取。"""
        _patch_latest(monkeypatch, "2026-09-30")
        called = []
        monkeypatch.setattr(
            mdb, "fetch_and_store_daily",
            lambda code, days, source="auto": called.append(code) or 0,
        )
        assert mdb.refresh_stale_tail("600900") is False
        assert called == []

    def test_no_local_data_skips_fetch(self, effective_today, monkeypatch):
        """本地无任何数据（latest=None）→ 不拉取（补尾只补尾部，不负责冷启动）。"""
        _patch_latest(monkeypatch, None)
        called = []
        monkeypatch.setattr(
            mdb, "fetch_and_store_daily",
            lambda code, days, source="auto": called.append(code) or 0,
        )
        assert mdb.refresh_stale_tail("600900") is False
        assert called == []

    def test_cooldown_blocks_second_attempt(self, effective_today, monkeypatch):
        """同 code 冷却期内第二次调用直接返回 False，不再打数据源。"""
        _patch_latest(monkeypatch, "2026-09-23")
        calls = []
        monkeypatch.setattr(
            mdb, "fetch_and_store_daily",
            lambda code, days, source="auto": calls.append(code) or 100,
        )
        assert mdb.refresh_stale_tail("600900") is True
        assert mdb.refresh_stale_tail("600900") is False
        assert len(calls) == 1

    def test_fetch_failure_is_silent(self, effective_today, monkeypatch):
        """拉取抛错 → 静默返回 False，不向上抛。"""
        _patch_latest(monkeypatch, "2026-09-23")

        def _boom(code, days, source="auto"):
            raise RuntimeError("network error")

        monkeypatch.setattr(mdb, "fetch_and_store_daily", _boom)
        assert mdb.refresh_stale_tail("600900") is False

    def test_failed_attempt_also_occupies_cooldown(self, effective_today, monkeypatch):
        """失败也记冷却时间：下一次调用在冷却内不再重试（防热路径反复打源）。"""
        _patch_latest(monkeypatch, "2026-09-23")
        calls = []

        def _boom(code, days, source="auto"):
            calls.append(code)
            raise RuntimeError("network error")

        monkeypatch.setattr(mdb, "fetch_and_store_daily", _boom)
        assert mdb.refresh_stale_tail("600900") is False
        assert mdb.refresh_stale_tail("600900") is False
        assert len(calls) == 1

    def test_injected_latest_and_fetch(self, effective_today, monkeypatch):
        """注入 latest/fetch（图表路由指数/板块等场景）时不查库、走注入通道。"""
        called = []
        monkeypatch.setattr(
            mdb, "get_latest_trade_date",
            lambda code: pytest.fail("注入 latest 时不应查库"),
        )
        monkeypatch.setattr(
            mdb, "fetch_and_store_daily",
            lambda code, days, source="auto": pytest.fail("不应用默认 fetch"),
        )
        fetch = lambda code, days: called.append((code, days)) or 10
        assert mdb.refresh_stale_tail("000300", latest="2026-06-05", fetch=fetch) is True
        assert called and called[0][0] == "000300"

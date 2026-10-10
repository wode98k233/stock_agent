"""K 线增量自动补齐测试（server.routes.charts._maybe_refresh_daily）。

场景：7 月查过 513330，本地缓存停在 7 月；9 月再查应只同步缺口天数
（INSERT OR REPLACE 幂等覆盖），而非全量重拉；失败静默用本地旧数据。
"""
import pandas as pd
import pytest

from server.routes import charts


def _df(latest_date: str) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": ["2026-06-01", "2026-06-02", latest_date]})


@pytest.fixture(autouse=True)
def clear_locks():
    from utils.cache import market_data_db as mdb
    mdb._tail_refresh_last.clear()
    yield
    mdb._tail_refresh_last.clear()


@pytest.fixture
def fresh_today(monkeypatch):
    """mock 有效交易日为今天。"""
    from datetime import date
    monkeypatch.setattr(
        "utils.trading_calendar.get_effective_trading_date",
        lambda market: date(2026, 9, 3),
    )


class TestMaybeRefreshDaily:
    def test_fresh_data_skips_sync(self, fresh_today, monkeypatch):
        """本地最新 = 有效交易日 → 不同步。"""
        called = []
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_daily",
            lambda code, days, source="auto": called.append(code) or 0,
        )
        assert charts._maybe_refresh_daily("513330", _df("2026-09-03")) is False
        assert called == []

    def test_stale_data_syncs_gap_only(self, fresh_today, monkeypatch):
        """本地停在 7 月 → 触发同步，且拉取天数为缺口（非全量 250）。"""
        captured = {}
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_daily",
            lambda code, days, source="auto": captured.update(code=code, days=days) or 100,
        )
        assert charts._maybe_refresh_daily("513330", _df("2026-07-07")) is True
        assert captured["code"] == "513330"
        # 缺口约 58 个自然日 + 15 余量 → 73；且远小于全量 250
        assert 40 <= captured["days"] < 250

    def test_min_40_days(self, fresh_today, monkeypatch):
        """缺口很小时（如昨天）也至少拉 40 天，保证数据源返回行数达标。"""
        captured = {}
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_daily",
            lambda code, days, source="auto": captured.update(days=days) or 50,
        )
        assert charts._maybe_refresh_daily("513330", _df("2026-09-02")) is True
        assert captured["days"] >= 40

    def test_rate_limit_within_interval(self, fresh_today, monkeypatch):
        """同 code 在最小间隔内只尝试一次（防高频查询反复触发网络同步）。"""
        calls = []
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_daily",
            lambda code, days, source="auto": calls.append(code) or 100,
        )
        assert charts._maybe_refresh_daily("513330", _df("2026-07-07")) is True
        assert charts._maybe_refresh_daily("513330", _df("2026-07-07")) is False
        assert len(calls) == 1

    def test_sync_failure_is_silent(self, fresh_today, monkeypatch):
        """同步失败（数据源异常）静默返回 False，调用方继续用本地旧数据。"""
        def _boom(code, days, source="auto"):
            raise RuntimeError("network error")
        monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_daily", _boom)
        assert charts._maybe_refresh_daily("513330", _df("2026-07-07")) is False

    def test_bad_date_is_silent(self, fresh_today):
        """本地日期格式异常时不抛错。"""
        assert charts._maybe_refresh_daily("513330", _df("not-a-date")) is False


class TestMaybeRefreshIndexDaily:
    """指数日线增量补齐（P0：index_daily 停更导致 signal_eval alpha 基准错位）。"""

    def test_stale_index_triggers_sync(self, fresh_today, monkeypatch):
        """本地指数停在 6 月 → 触发 fetch_and_store_index_daily 增量同步。"""
        captured = {}
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_index_daily",
            lambda code, days: captured.update(code=code, days=days) or 100,
        )
        assert charts._maybe_refresh_index_daily("000300", _df("2026-06-05")) is True
        assert captured["code"] == "000300"
        assert 40 <= captured["days"] < 250

    def test_fresh_index_skips_sync(self, fresh_today, monkeypatch):
        """本地指数最新 = 有效交易日 → 不同步。"""
        called = []
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_index_daily",
            lambda code, days: called.append(code) or 0,
        )
        assert charts._maybe_refresh_index_daily("000300", _df("2026-09-03")) is False
        assert called == []

    def test_index_sync_failure_is_silent(self, fresh_today, monkeypatch):
        """指数同步失败静默返回 False（继续用本地旧数据）。"""
        def _boom(code, days):
            raise RuntimeError("network error")
        monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_index_daily", _boom)
        assert charts._maybe_refresh_index_daily("000300", _df("2026-06-05")) is False

    def test_index_rate_limit_within_interval(self, fresh_today, monkeypatch):
        """同指数在最小间隔内只尝试一次。"""
        calls = []
        monkeypatch.setattr(
            "utils.cache.market_data_db.fetch_and_store_index_daily",
            lambda code, days: calls.append(code) or 100,
        )
        assert charts._maybe_refresh_index_daily("000300", _df("2026-06-05")) is True
        assert charts._maybe_refresh_index_daily("000300", _df("2026-06-05")) is False
        assert len(calls) == 1

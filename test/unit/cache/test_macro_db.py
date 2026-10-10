"""宏观指标长表测试 — macro_db：DDL + 增量同步 + 查询（临时 fundamental_cache.db）。"""
import sqlite3
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


@pytest.fixture
def macro_db(tmp_path):
    """临时 fundamental_cache.db + 初始化 macro 表。"""
    from utils.cache import fundamental_db, macro_db as mdb
    db_path = str(tmp_path / "fundamental_cache.db")
    mock_config = MagicMock()
    mock_config.get_fundamental_db_path.return_value = db_path
    with patch.object(fundamental_db, "Config", mock_config):
        mdb.init_macro_tables()
        yield mdb


def _df_m2():
    return pd.DataFrame({
        "report_date": ["2026-07-31", "2026-08-31"],
        "value": [6.2, 6.1],
    })


class TestDDL:
    def test_table_created(self, macro_db, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "fundamental_cache.db"))
        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "macro_indicator" in tables

    def test_registered_in_core_init(self):
        """macro_indicator 由统一 schema 覆盖（registry fundamental 组，init_cache_tables 走 registry）。"""
        from utils.db_schema.registry import ALL_DBS
        names = [t.name for t in ALL_DBS["fundamental"].tables]
        assert "macro_indicator" in names


class TestFetchAndStore:
    def test_sync_stores_rows(self, macro_db):
        fetcher = MagicMock(return_value=_df_m2())
        result = macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)
        assert result["m2_yoy"]["added"] == 2
        rows = macro_db.get_macro_series("m2_yoy", periods=12)
        assert len(rows) == 2
        assert rows[-1]["report_date"] == "2026-08-31"
        assert rows[-1]["value"] == pytest.approx(6.1)

    def test_sync_idempotent(self, macro_db):
        fetcher = MagicMock(return_value=_df_m2())
        macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)
        result = macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)
        assert result["m2_yoy"]["added"] == 0  # 全部已存在
        assert len(macro_db.get_macro_series("m2_yoy")) == 2

    def test_incremental_only_new_rows(self, macro_db):
        """库内已有 07/08，fetcher 返回 08/09 → 只写 09 一行。"""
        fetcher = MagicMock(return_value=_df_m2())
        macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)

        fetcher.return_value = pd.DataFrame({
            "report_date": ["2026-08-31", "2026-09-30"],
            "value": [6.1, 6.0],
        })
        result = macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)
        assert result["m2_yoy"]["added"] == 1
        rows = macro_db.get_macro_series("m2_yoy")
        assert len(rows) == 3

    def test_single_failure_does_not_block_batch(self, macro_db):
        """单指标失败不阻断整批（返回 error 标记，其余照常入库）。"""
        def flaky(indicator):
            if indicator == "cpi_yoy":
                raise RuntimeError("金十接口挂了")
            return pd.DataFrame({"report_date": ["2026-08-31"], "value": [3.0]})
        result = macro_db.fetch_and_store_macro(
            ["lpr_1y", "cpi_yoy"], fetcher=flaky)
        assert "error" in result["cpi_yoy"]
        assert result["lpr_1y"]["added"] == 1
        assert len(macro_db.get_macro_series("lpr_1y")) == 1

    def test_source_and_unit_persisted(self, macro_db):
        fetcher = MagicMock(return_value=_df_m2())
        macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)
        from utils.cache.fundamental_db import get_fundamental_db
        with get_fundamental_db() as conn:
            row = conn.execute(
                "SELECT source, unit, period_type FROM macro_indicator"
                " WHERE indicator_code='m2_yoy' LIMIT 1").fetchone()
        assert row["source"] == "akshare"
        assert row["unit"] == "%"
        assert row["period_type"] == "monthly"


class TestGetSeries:
    def test_periods_limit(self, macro_db):
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": [f"2025-{m:02d}-28" for m in range(1, 13)] + ["2026-08-31"],
            "value": list(range(13)),
        }))
        macro_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)
        rows = macro_db.get_macro_series("m2_yoy", periods=5)
        assert len(rows) == 5
        # 升序（旧→新）
        assert rows[0]["report_date"] < rows[-1]["report_date"]
        assert rows[-1]["report_date"] == "2026-08-31"

    def test_empty_returns_empty_list(self, macro_db):
        assert macro_db.get_macro_series("never_synced") == []


class TestMeta:
    def test_indicator_meta_covers_all_apis(self):
        """元数据表必须覆盖 akshare_ds._MACRO_APIS 全部指标（label/unit 供技能与前端用）。"""
        from tools.fetcher.akshare_ds import AkshareDataSource
        from utils.cache.macro_db import MACRO_INDICATOR_META
        assert set(MACRO_INDICATOR_META) >= set(AkshareDataSource._MACRO_APIS)
        for code, meta in MACRO_INDICATOR_META.items():
            assert meta["label"], code
            assert meta["unit"], code
            assert meta["period_type"] in ("monthly", "quarterly", "daily"), code

    def test_rate_indicators_registered(self):
        from utils.cache.macro_db import MACRO_INDICATOR_META
        for code in ("cn10_y", "cn30_y", "us10_y"):
            assert code in MACRO_INDICATOR_META
            assert MACRO_INDICATOR_META[code]["unit"] == "%"
            assert MACRO_INDICATOR_META[code]["period_type"] == "daily"


class TestStaleThreshold:
    """滞后阈值必须按周期分档：统一 400 天会让断供数月的日频指标被当成当日读数。"""

    def test_daily_indicators_use_tight_threshold(self):
        from utils.cache.macro_db import stale_threshold_days
        assert stale_threshold_days("cn10_y") <= 15
        assert stale_threshold_days("us10_y") <= 15

    def test_lpr_uses_monthly_threshold(self):
        """LPR 每月 20 日公布一次，「日频」阈值(10 天)会把正常月频误报为陈旧。"""
        from utils.cache.macro_db import stale_threshold_days
        assert stale_threshold_days("lpr_1y") == 45
        assert stale_threshold_days("lpr_5y") == 45

    def test_jin10_series_keep_loose_threshold(self):
        """金十系源本身滞后约 1 年，阈值不能收紧，否则永远判 stale。"""
        from utils.cache.macro_db import stale_threshold_days
        for code in ("cpi_yoy", "gdp_yoy", "pmi_mfg"):
            assert stale_threshold_days(code) == 400

    def test_period_defaults(self):
        from utils.cache.macro_db import stale_threshold_days
        assert stale_threshold_days("m2_yoy") == 45   # 月频
        assert stale_threshold_days("gdp_yoy") == 400  # 季频被金十覆盖

    def test_unknown_indicator_falls_back_loose(self):
        from utils.cache.macro_db import stale_threshold_days
        assert stale_threshold_days("nope") == 400


class TestUpsertIndicatorSeries:
    """非宏观源（中证指数公司股息率/PE）复用同一张长表。"""

    _META = {"unit": "%", "period_type": "daily"}

    def test_upsert_arbitrary_code(self, macro_db):
        df = pd.DataFrame({"report_date": ["2026-09-15", "2026-09-16"],
                           "value": [4.40, 4.38]})
        added = macro_db.upsert_indicator_series(
            "idx_h30269_dy", self._META, df, source="csindex")
        assert added == 2
        rows = macro_db.get_macro_series("idx_h30269_dy")
        assert [r["report_date"] for r in rows] == ["2026-09-15", "2026-09-16"]
        assert rows[-1]["value"] == pytest.approx(4.38)

    def test_incremental_only_appends_new_dates(self, macro_db):
        macro_db.upsert_indicator_series(
            "idx_h30269_dy", self._META,
            pd.DataFrame({"report_date": ["2026-09-15", "2026-09-16"], "value": [4.4, 4.38]}),
            source="csindex")
        added = macro_db.upsert_indicator_series(
            "idx_h30269_dy", self._META,
            pd.DataFrame({"report_date": ["2026-09-16", "2026-09-17"], "value": [4.38, 4.30]}),
            source="csindex")
        assert added == 1
        assert macro_db.get_latest_indicator("idx_h30269_dy")["value"] == pytest.approx(4.30)

    def test_source_recorded(self, macro_db, tmp_path):
        macro_db.upsert_indicator_series(
            "idx_h30269_dy", self._META,
            pd.DataFrame({"report_date": ["2026-09-16"], "value": [4.38]}),
            source="csindex")
        conn = sqlite3.connect(str(tmp_path / "fundamental_cache.db"))
        sources = {r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM macro_indicator")}
        conn.close()
        assert sources == {"csindex"}

    def test_empty_df_is_noop(self, macro_db):
        assert macro_db.upsert_indicator_series(
            "idx_h30269_dy", self._META, pd.DataFrame(), source="csindex") == 0

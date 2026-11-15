"""标的别名查询层（backtest/symbol_alias.py）：数据来自 symbol_alias 表，代码只查询。

别名数据本身由 `test/unit/test_no_hardcoded_symbol_aliases.py` 守卫（全库扫描 + 数据契约），
这里只测查询语义：kind 隔离、行序、展示名挑选、文本命中优先级，以及 P1 的
「缺库/空表自动补种」与「补种失败时显式降级（不静默）」。
"""
from __future__ import annotations

import pytest

import backtest.symbol_alias as SA


pytestmark = pytest.mark.usefixtures("symbol_alias_db")


class TestLookupOne:
    def test_kind_isolates_same_alias(self):
        """「沪深300」既是指数又是 ETF 简称：靠 kind 区分，互不污染。"""
        assert SA.lookup_one("沪深300", kind="index") == "000300"
        assert SA.lookup_one("沪深300", kind="etf") == "510300"

    def test_default_kind_is_etf(self):
        assert SA.lookup_one("红利ETF") == "510880"

    def test_case_insensitive(self):
        assert SA.lookup_one("cpo") == SA.lookup_one("CPO") == "515880"
        assert SA.lookup_one("HS300", kind="index") == "000300"

    def test_unknown_returns_none(self):
        assert SA.lookup_one("量子ETF") is None
        assert SA.lookup_one("") is None


class TestAutoSeed:
    """P1（2026-11-01）：`db/` 不进 git，缺库/空表必须自动补种，不许静默查不到。

    静默查不到的后果不是「少一个命中」，而是名称被当代码：真机复现
    `跑沪深300` → `POOL:codes:000300`（指数被当个股回测）。
    """

    def test_missing_db_is_auto_seeded(self, tmp_path, monkeypatch):
        db = tmp_path / "fresh" / "market_data.db"
        monkeypatch.setattr(SA, "_db_path", lambda: str(db))
        SA._READY.clear()
        assert SA.is_ready() is True
        assert db.exists()
        assert SA._alias_row_count(str(db)) == len(SA._load_seed_rows())
        # 补种后查询就是正常的
        assert SA.lookup_one("沪深300", kind="index") == "000300"
        assert SA.lookup_one("沪深300", kind="etf") == "510300"
        assert SA.find_alias_in_text("跑沪深300") == ("沪深300", "000300")

    def test_empty_table_is_auto_seeded(self, tmp_path, monkeypatch):
        import sqlite3

        from utils.db_schema.migrator import ensure_schema
        from utils.db_schema.tables import market_data as md

        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        try:
            ensure_schema(conn, md.TABLES, [])        # 表在、行没有
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(SA, "_db_path", lambda: str(db))
        SA._READY.clear()
        assert SA.lookup_one("红利ETF") == "510880"
        assert SA._alias_row_count(str(db)) > 0

    def test_unreadable_seed_degrades_loudly(self, tmp_path, monkeypatch):
        """连数据文件都没有 → 查不到（返回 None/[]），但必须显式告警 + require_ready 抛错。"""
        db = tmp_path / "nope" / "market_data.db"
        monkeypatch.setattr(SA, "_db_path", lambda: str(db))
        monkeypatch.setattr(SA, "SEED_PATH", tmp_path / "missing_seed.json")
        SA._READY.clear()

        assert SA.lookup_one("红利ETF") is None
        assert SA.lookup_group("易中天") == []
        assert SA.list_aliases("etf") == []
        assert SA.lookup_label("000300") == ""
        assert SA.find_alias_in_text("沪深300") is None
        assert SA.is_ready() is False
        with pytest.raises(SA.SymbolAliasNotReady):
            SA.require_ready()

    def test_require_ready_passes_when_seeded(self):
        assert SA.is_ready() is True
        SA.require_ready()          # 不抛


class TestLookupGroup:
    def test_synonym_group_keeps_order(self):
        assert SA.lookup_group("易中天") == ["300502", "300308", "300394"]

    def test_single_alias_is_not_a_group(self):
        assert SA.lookup_group("红利ETF") == []


class TestListAliases:
    def test_kind_filter_and_seed_order(self):
        rows = SA.list_aliases("index")
        assert [a for a, _ in rows] == ["沪深300", "沪深三百", "hs300",
                                        "中证500", "中证五百", "zz500"]
        assert all(c in ("000300", "000905") for _, c in rows)

    def test_unknown_kind_is_empty(self):
        assert SA.list_aliases("no_such_kind") == []


class TestLookupLabel:
    def test_prefers_chinese_alias(self):
        """展示名只该是中文全称那条（沪深300），不是 沪深三百/hs300。"""
        assert SA.lookup_label("000300") == "沪深300"
        assert SA.lookup_label("000905") == "中证500"

    def test_unknown_code_returns_empty(self):
        assert SA.lookup_label("999999") == ""

    def test_kind_filter(self):
        assert SA.lookup_label("510300", kind="etf") == "沪深300ETF"
        assert SA.lookup_label("000300", kind="etf") == ""


class TestFindAliasInText:
    def test_finds_alias(self):
        assert SA.find_alias_in_text("沪深300成分股里验证") == ("沪深300", "000300")

    def test_longest_alias_wins(self):
        """同一 kind 内长别名优先（ETF 序里「沪深300ETF」先于「沪深300」）。"""
        assert SA.find_alias_in_text("跑沪深300ETF", kind="etf") == ("沪深300ETF", "510300")

    def test_find_aliases_in_text_returns_all_ordered(self):
        hits = SA.find_aliases_in_text("沪深300ETF和沪深300", kind="etf")
        assert hits[0] == ("沪深300ETF", "510300")
        assert ("沪深300", "510300") in hits

    def test_index_kind_only_sees_index_rows(self):
        """kind='index' 里没有「沪深300ETF」这一行（它属于 kind='etf'）。"""
        assert SA.find_alias_in_text("跑沪深300ETF", kind="index") == ("沪深300", "000300")

    def test_equal_length_follows_seed_order(self):
        # 与旧实现（elif 链里 沪深300 先于 中证500）保持一致
        assert SA.find_alias_in_text("中证500和沪深300都跑") == ("沪深300", "000300")

    def test_case_insensitive(self):
        assert SA.find_alias_in_text("ZZ500 里跑") == ("zz500", "000905")

    def test_no_hit(self):
        assert SA.find_alias_in_text("医药板块") is None
        assert SA.find_alias_in_text("") is None

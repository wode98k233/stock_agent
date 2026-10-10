"""裸码 → 取数表 的标的类型判定（`resolve_symbol_market` / `is_index_code`）。

事故背景（2026-09-30）：`000001` 在 `index_daily`/`index_info` 是**上证指数**，
在 `stock_daily`/`stock_info` 是**平安银行**——同码不同标的，两表各存一份。
tech_panel 只带裸码不带类型时，指数会被解析成同码个股（价格、名称全错且不报错）。
实测 `index_info ∩ stock_info` **只有 000001**。

口径：歧义码默认按个股（保持既有行为），由调用方按场景（市场类报告）显式指定
INDEX；名称命中 index_info 时最可信 → INDEX。
"""
import pytest

from utils.cache.market_data_db import (
    get_index_daily_count,
    init_market_data_tables,
    is_index_code,
    resolve_symbol_market,
    upsert_index_daily,
    upsert_index_info,
    upsert_stock_info,
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    from config import Config

    db_path = tmp_path / "market_data.db"
    monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))
    init_market_data_tables()
    upsert_index_info([{"code": "000001", "name": "上证指数"},
                       {"code": "399001", "name": "深证成指"}])
    upsert_stock_info({"code": "000001", "name": "平安银行", "market": "SZ"})
    upsert_stock_info({"code": "601398", "name": "工商银行", "market": "SH"})
    yield


class TestResolveSymbolMarket:
    def test_ambiguous_code_defaults_to_stock(self):
        """歧义码（既在指数表又在个股表）默认按个股 —— 保持既有行为。"""
        assert resolve_symbol_market("000001") == "STOCK"
        assert resolve_symbol_market("000001", "平安银行") == "STOCK"

    def test_name_hint_identifies_index(self):
        """名称命中 index_info → INDEX（fragment 的 stock_name 来自数据源表头）。"""
        assert resolve_symbol_market("000001", "上证指数") == "INDEX"

    def test_index_only_code(self):
        """无同码个股的指数（399001 深证成指）→ INDEX，带不带 hint 都一样。"""
        assert resolve_symbol_market("399001") == "INDEX"
        assert resolve_symbol_market("399001", "深证成指") == "INDEX"

    def test_stock_only_code(self):
        assert resolve_symbol_market("601398") == "STOCK"
        assert resolve_symbol_market("601398", "工商银行") == "STOCK"

    def test_unknown_and_empty_code(self):
        assert resolve_symbol_market("") == "STOCK"
        assert resolve_symbol_market("999999") == "STOCK"

    def test_is_index_code(self, monkeypatch):
        assert is_index_code("000001") is True      # 歧义码也算「是指数码」
        assert is_index_code("399001") is True
        assert is_index_code("601398") is False
        assert is_index_code("") is False

    def test_is_index_code_never_raises(self, monkeypatch):
        """DB 异常时返回 False，绝不冒泡（调用方在报告主链路上）。"""
        from utils.cache import market_data_db as mdb

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(mdb, "get_market_data_db", boom)
        assert is_index_code("000001") is False


class TestIndexDailyCount:
    def test_count(self):
        assert get_index_daily_count("000001") == 0
        upsert_index_daily([{"code": "000001", "trade_date": "2026-09-30", "close": 3842.19}])
        assert get_index_daily_count("000001") == 1
        assert get_index_daily_count("399001") == 0

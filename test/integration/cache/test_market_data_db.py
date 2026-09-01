"""market_data.db 集成测试

测试 market_data.db 的完整读写流程：
- stock_daily CRUD
- stock_info CRUD
- index_daily CRUD
- 数据自愈 fetch_and_store_daily
"""
import pytest
import pandas as pd
from datetime import datetime

from utils.cache.market_data_db import (
    init_market_data_tables,
    # stock_daily
    upsert_stock_daily,
    get_stock_daily,
    get_latest_trade_date,
    get_stock_daily_count,
    # stock_info
    upsert_stock_info,
    get_stock_info,
    # index_daily
    upsert_index_daily,
    get_index_daily,
    upsert_index_info,
    # helpers
    _detect_market,
    _normalize_trade_date,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


@pytest.fixture(autouse=True)
def _init_and_cleanup():
    """确保表结构已初始化，测试后清理测试数据"""
    init_market_data_tables()
    yield
    # 清理测试用数据
    from utils.cache.market_data_db import get_market_data_db
    with get_market_data_db() as conn:
        for code in ["TEST001", "TEST002", "TEST003", "TEST004", "TEST005", "TEST006",
                      "INFO001", "INFO002"]:
            conn.execute("DELETE FROM stock_daily WHERE code = ?", (code,))
            conn.execute("DELETE FROM stock_info WHERE code = ?", (code,))
        conn.execute("DELETE FROM index_daily WHERE code = '000300' AND source = 'test'")
        conn.execute("DELETE FROM index_info WHERE source = 'test'")


# ── stock_daily CRUD ────────────────────────────────────────

class TestStockDaily:
    """日线数据 CRUD"""

    def test_upsert_and_query(self):
        """写入并读回"""
        records = [
            {"code": "TEST001", "trade_date": "2025-06-01", "open": 10.0, "high": 11.0,
             "low": 9.5, "close": 10.5, "volume": 1000000, "amount": 10500000},
            {"code": "TEST001", "trade_date": "2025-06-02", "open": 10.5, "high": 11.5,
             "low": 10.0, "close": 11.0, "volume": 1200000, "amount": 13200000},
        ]
        count = upsert_stock_daily(records, source="test")
        assert count == 2

        df = get_stock_daily("TEST001", start_date="2025-06-01", end_date="2025-06-02")
        assert len(df) == 2
        assert df.iloc[0]["close"] == 10.5
        assert df.iloc[1]["close"] == 11.0

    def test_upsert_overwrite(self):
        """重复写入覆盖"""
        records = [
            {"code": "TEST002", "trade_date": "2025-06-01", "open": 10.0, "high": 11.0,
             "low": 9.5, "close": 10.5, "volume": 1000000},
        ]
        upsert_stock_daily(records, source="test")

        # 覆盖
        records[0]["close"] = 12.0
        upsert_stock_daily(records, source="test")

        df = get_stock_daily("TEST002", start_date="2025-06-01", end_date="2025-06-01")
        assert len(df) == 1
        assert df.iloc[0]["close"] == 12.0

    def test_query_nonexistent_code(self):
        """查询不存在的股票返回空"""
        df = get_stock_daily("NONEXISTENT_999")
        assert df.empty

    def test_query_with_limit(self):
        """带 limit 查询"""
        records = [
            {"code": "TEST003", "trade_date": f"2025-06-{i:02d}", "open": 10.0, "high": 11.0,
             "low": 9.5, "close": 10.0 + i * 0.1, "volume": 1000000}
            for i in range(1, 6)
        ]
        upsert_stock_daily(records, source="test")

        df = get_stock_daily("TEST003", limit=3)
        assert len(df) == 3

    def test_latest_trade_date(self):
        """获取最新交易日期"""
        records = [
            {"code": "TEST004", "trade_date": "2025-06-01", "open": 10, "high": 11,
             "low": 9, "close": 10, "volume": 100},
            {"code": "TEST004", "trade_date": "2025-06-03", "open": 10, "high": 11,
             "low": 9, "close": 10, "volume": 100},
        ]
        upsert_stock_daily(records, source="test")

        latest = get_latest_trade_date("TEST004")
        assert latest == "2025-06-03"

    def test_daily_count(self):
        """获取数据条数"""
        records = [
            {"code": "TEST005", "trade_date": f"2025-06-{i:02d}", "open": 10, "high": 11,
             "low": 9, "close": 10, "volume": 100}
            for i in range(1, 4)
        ]
        upsert_stock_daily(records, source="test")
        assert get_stock_daily_count("TEST005") == 3

    def test_query_date_range(self):
        """日期范围查询"""
        records = [
            {"code": "TEST006", "trade_date": f"2025-06-{i:02d}", "open": 10, "high": 11,
             "low": 9, "close": 10, "volume": 100}
            for i in range(1, 11)
        ]
        upsert_stock_daily(records, source="test")

        df = get_stock_daily("TEST006", start_date="2025-06-03", end_date="2025-06-07")
        assert len(df) == 5
        assert df.iloc[0]["trade_date"] == "2025-06-03"
        assert df.iloc[-1]["trade_date"] == "2025-06-07"

    def test_empty_records(self):
        """空列表写入返回 0"""
        assert upsert_stock_daily([], source="test") == 0


# ── stock_info CRUD ─────────────────────────────────────────

class TestStockInfo:
    """股票信息 CRUD"""

    def test_upsert_and_get(self):
        """写入并读取"""
        upsert_stock_info({
            "code": "INFO001",
            "name": "测试股票",
            "market": "SH",
            "status": "active",
        }, source="test")

        info = get_stock_info("INFO001")
        assert info is not None
        assert info["name"] == "测试股票"
        assert info["market"] == "SH"

    def test_upsert_overwrite_info(self):
        """覆盖更新"""
        upsert_stock_info({"code": "INFO002", "name": "旧名称", "market": "SZ"}, source="test")
        upsert_stock_info({"code": "INFO002", "name": "新名称", "market": "SZ"}, source="test")

        info = get_stock_info("INFO002")
        assert info["name"] == "新名称"

    def test_get_nonexistent_info(self):
        """不存在返回 None"""
        assert get_stock_info("NONEXISTENT_999") is None


# ── index_daily CRUD ────────────────────────────────────────

class TestIndexDaily:
    """指数日线 CRUD"""

    def test_upsert_and_query_index(self):
        """写入并读取指数日线"""
        records = [
            {"code": "000300", "trade_date": "2025-06-01", "open": 4000, "high": 4050,
             "low": 3980, "close": 4020, "volume": 100000000, "amount": 50000000000},
            {"code": "000300", "trade_date": "2025-06-02", "open": 4020, "high": 4100,
             "low": 4010, "close": 4080, "volume": 120000000, "amount": 60000000000},
        ]
        count = upsert_index_daily(records, source="test")
        assert count == 2

        df = get_index_daily("000300", start_date="2025-06-01", end_date="2025-06-02")
        assert len(df) == 2
        assert df.iloc[0]["close"] == 4020

    def test_upsert_index_info(self):
        """写入指数信息"""
        upsert_index_info([{"code": "000300", "name": "沪深300"}], source="test")
        # 不报错即可，index_info 的 get 接口不在 market_data_db 中

    def test_query_empty_index(self):
        """查询不存在的指数返回空"""
        df = get_index_daily("999999")
        assert df.empty


# ── 辅助函数 ────────────────────────────────────────────────

class TestHelpers:
    """辅助函数"""

    def test_detect_market_sh(self):
        """沪市股票"""
        assert _detect_market("600519") == "SH"
        assert _detect_market("601398") == "SH"

    def test_detect_market_sz(self):
        """深市股票"""
        assert _detect_market("000001") == "SZ"
        assert _detect_market("300750") == "SZ"

    def test_detect_market_bj(self):
        """北交所"""
        assert _detect_market("430047") == "BJ"
        assert _detect_market("830799") == "BJ"

    def test_detect_market_etf(self):
        """ETF"""
        assert _detect_market("510300") == "ETF"
        assert _detect_market("159915") == "ETF"

    def test_detect_market_board(self):
        """板块"""
        assert _detect_market("board_industry_黄金") == "board"

    def test_normalize_trade_date_string(self):
        """字符串日期标准化"""
        assert _normalize_trade_date("2025-06-01") == "2025-06-01"

    def test_normalize_trade_date_timestamp(self):
        """Timestamp 日期标准化"""
        ts = pd.Timestamp("2025-06-01")
        assert _normalize_trade_date(ts) == "2025-06-01"

    def test_normalize_trade_date_compact(self):
        """紧凑格式日期标准化"""
        result = _normalize_trade_date("20250601")
        assert result == "2025-06-01"


# ── fetch_and_store_daily ───────────────────────────────────

class TestFetchAndStore:
    """数据采集集成"""

    def test_fetch_real_stock(self):
        """真实股票数据采集（需要网络）"""
        from utils.cache.market_data_db import fetch_and_store_daily
        try:
            count = fetch_and_store_daily("600519", days=30)
            assert count > 0
            assert get_stock_daily_count("600519") > 0
        except ValueError as e:
            pytest.skip(f"数据采集失败（网络问题）: {e}")

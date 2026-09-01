"""
测试目标: agents/scenarios/data_query.py
覆盖范围:
  - _parse_query_type: 8 种查询类型 + 默认 all
  - _format_answer: 9 种格式化分支
  - handle_data_query: resolve_stock 空、data 空、正常流程
Mock 策略: mock resolve_stock、fetch_stock_bundle
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── _parse_query_type ────────────────────────────────────────

class TestParseQueryType:
    """_parse_query_type: 查询类型识别"""

    def test_price(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("茅台现在多少钱") == "price"
        assert _parse_query_type("现价是多少") == "price"
        assert _parse_query_type("最新价格") == "price"

    def test_pct_chg(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("今天涨幅多少") == "pct_chg"
        assert _parse_query_type("涨跌幅") == "pct_chg"

    def test_pe(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("PE是多少") == "pe"
        assert _parse_query_type("市盈率") == "pe"

    def test_pb(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("PB多少") == "pb"
        assert _parse_query_type("市净率") == "pb"

    def test_volume(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("成交量") == "volume"
        assert _parse_query_type("成交额") == "volume"

    def test_turnover(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("换手率") == "turnover"

    def test_market_cap(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("总市值") == "market_cap"

    def test_dividend(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("股息率") == "dividend"

    def test_default_all(self):
        from agents.scenarios.data_query import _parse_query_type
        assert _parse_query_type("分析一下茅台") == "all"
        assert _parse_query_type("") == "all"


# ── _format_answer ───────────────────────────────────────────

class TestFormatAnswer:
    """_format_answer: 格式化输出"""

    def _data(self, **overrides):
        base = {"price": 1800.0, "pct_chg": 2.5, "pe": 30.0, "pb": 10.0,
                "amount": 50000, "turnover_rate": 1.5, "total_mv": 2000000,
                "dividend_yield": 1.5}
        base.update(overrides)
        return base

    def test_price_query(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(), "price", "2026-01-01")
        assert "茅台" in result
        assert "600519" in result
        assert "1800" in result

    def test_pe_query_with_valid_pe(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(pe=30.5), "pe", "2026-01-01")
        assert "30.5" in result

    def test_pe_query_with_zero_pe(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(pe=0), "pe", "2026-01-01")
        assert "N/A" in result

    def test_pb_query_with_negative_pb(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(pb=-1), "pb", "2026-01-01")
        assert "N/A" in result

    def test_volume_query(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(amount=50000), "volume", "2026-01-01")
        assert "成交额" in result

    def test_market_cap_query(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(total_mv=2000000), "market_cap", "2026-01-01")
        assert "总市值" in result

    def test_all_query_shows_pe_when_positive(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(pe=30), "all", "2026-01-01")
        assert "PE" in result

    def test_all_query_hides_pe_when_zero(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(pe=0), "all", "2026-01-01")
        assert "PE" not in result

    def test_all_query_shows_amount_when_large(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(amount=50000), "all", "2026-01-01")
        assert "成交额" in result

    def test_all_query_hides_amount_when_small(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", self._data(amount=5000), "all", "2026-01-01")
        assert "成交额" not in result

    def test_missing_keys_use_default(self):
        from agents.scenarios.data_query import _format_answer
        result = _format_answer("茅台", "600519", {}, "price", "2026-01-01")
        assert "茅台" in result


# ── handle_data_query ────────────────────────────────────────

class TestHandleDataQuery:
    """handle_data_query: 完整流程"""

    @pytest.mark.asyncio
    async def test_no_stock_returns_none(self):
        from agents.scenarios.data_query import handle_data_query
        with patch("agents.scenarios.data_query.resolve_stock", return_value=(None, None)):
            result = await handle_data_query("随便问", "", {}, "2026-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_data_returns_none(self):
        from agents.scenarios.data_query import handle_data_query
        with patch("agents.scenarios.data_query.resolve_stock", return_value=("600519", "茅台")), \
             patch("agents.scenarios.data_query.fetch_stock_bundle", new_callable=AsyncMock, return_value={"realtime": {}}):
            result = await handle_data_query("茅台多少钱", "", {}, "2026-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_normal_returns_answer(self):
        from agents.scenarios.data_query import handle_data_query
        with patch("agents.scenarios.data_query.resolve_stock", return_value=("600519", "茅台")), \
             patch("agents.scenarios.data_query.fetch_stock_bundle", new_callable=AsyncMock,
                   return_value={"realtime": {"price": 1800.0, "pct_chg": 2.5}}):
            result = await handle_data_query("茅台多少钱", "", {}, "2026-01-01")
        assert result is not None
        assert "茅台" in result.text
        assert "1800" in result.text

"""
测试目标: agents/scenarios/market_overview.py
覆盖范围:
  - _extract_index_data: 指数数据提取
  - _compute_breadth: 涨跌统计
  - handle_market_overview: 异常降级、空数据
Mock 策略: mock ak_spot_em、get_industry_list
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import pandas as pd


class TestExtractIndexData:
    """_extract_index_data: 指数提取"""

    def test_extracts_main_indices(self):
        from agents.scenarios.market_overview import _extract_index_data
        df = pd.DataFrame([
            {"名称": "上证指数", "最新价": 3000, "涨跌幅": 1.5},
            {"名称": "深证成指", "最新价": 10000, "涨跌幅": 2.0},
            {"名称": "创业板指", "最新价": 2000, "涨跌幅": 3.0},
        ])
        result = _extract_index_data(df)
        assert len(result) == 3
        names = [r["name"] for r in result]
        assert "上证指数" in names

    def test_no_matching_indices(self):
        from agents.scenarios.market_overview import _extract_index_data
        df = pd.DataFrame([
            {"名称": "其他指数", "最新价": 100, "涨跌幅": 0.5},
        ])
        result = _extract_index_data(df)
        assert len(result) == 0

    def test_empty_df(self):
        from agents.scenarios.market_overview import _extract_index_data
        df = pd.DataFrame(columns=["名称", "最新价", "涨跌幅"])
        result = _extract_index_data(df)
        assert len(result) == 0


class TestComputeBreadth:
    """_compute_breadth: 涨跌统计"""

    def test_normal(self):
        from agents.scenarios.market_overview import _compute_breadth
        df = pd.DataFrame({"涨跌幅": [1.0, -1.0, 2.0, -0.5, 0.0]})
        result = _compute_breadth(df)
        assert result["up"] == 2
        assert result["down"] == 2
        assert result["flat"] == 1

    def test_missing_column(self):
        from agents.scenarios.market_overview import _compute_breadth
        df = pd.DataFrame({"其他列": [1, 2, 3]})
        result = _compute_breadth(df)
        assert result["up"] == 0

    def test_empty_df(self):
        from agents.scenarios.market_overview import _compute_breadth
        df = pd.DataFrame(columns=["涨跌幅"])
        result = _compute_breadth(df)
        assert result["up"] == 0


class TestHandleMarketOverview:
    """handle_market_overview: 完整流程"""

    @pytest.mark.asyncio
    async def test_ak_spot_em_exception_returns_none(self):
        from agents.scenarios.market_overview import handle_market_overview
        with patch("tools.fetcher.ak_spot_em", side_effect=RuntimeError("API 失败")):
            result = await handle_market_overview("大盘", "", {}, "2026-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_df_returns_none(self):
        from agents.scenarios.market_overview import handle_market_overview
        with patch("tools.fetcher.ak_spot_em", return_value=pd.DataFrame()):
            result = await handle_market_overview("大盘", "", {}, "2026-01-01")
        assert result is None

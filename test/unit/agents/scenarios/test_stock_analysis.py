"""
测试目标: agents/scenarios/stock_analysis.py
覆盖范围:
  - handle_stock_analysis: resolve_stock 空、正常流程、情感分析异常
  - _format_output: 各维度输出（行情/技术/消息/基本面/综合判断）
Mock 策略: mock resolve_stock、fetch_stock_bundle、analyze_sentiment、call_llm
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def _make_bundle(**overrides):
    base = {
        "realtime": {"price": 1800.0, "pct_chg": 2.5, "amount": 50000, "turnover_rate": 1.5, "pe": 30, "pb": 10},
        "history": None,
        "news": [{"title": "利好消息", "time": "2026-01-01", "source": "财联社"}],
        "rating": {"rating_summary": "买入"},
    }
    base.update(overrides)
    return base


class TestHandleStockAnalysis:
    """handle_stock_analysis: 完整流程"""

    @pytest.mark.asyncio
    async def test_no_stock_returns_none(self):
        from agents.scenarios.stock_analysis import handle_stock_analysis
        with patch("agents.scenarios.common.resolve_stock", return_value=(None, None)):
            result = await handle_stock_analysis("随便问", "", {}, "2026-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_normal_returns_result(self):
        from agents.scenarios.stock_analysis import handle_stock_analysis
        with patch("agents.scenarios.common.resolve_stock", return_value=("600519", "茅台")), \
             patch("agents.scenarios.common.fetch_stock_bundle", new_callable=AsyncMock,
                   return_value=_make_bundle()), \
             patch("agents.scenarios.common.BaseScenarioHandler.calc_tech_indicators",
                   return_value={"macd": 0.5, "rsi_14": 60}), \
             patch("tools.sentiment.analyze_sentiment",
                   return_value={"total_score": 0.8, "conclusion": "正面", "summary": "利好"}), \
             patch("agents.scenarios.common.BaseScenarioHandler.call_llm",
                   new_callable=AsyncMock, return_value={
                       "tech_analysis": "MACD金叉", "news_summary": "利好消息",
                       "fundamental": "PE合理", "conclusion_short": "看涨",
                       "conclusion_mid": "中性", "risk": "低"
                   }):
            result = await handle_stock_analysis("分析茅台", "分析茅台", {}, "2026-01-01")
        assert result is not None
        assert result.text is not None
        assert "茅台" in result.text
        assert result.data["stock_code"] == "600519"

    @pytest.mark.asyncio
    async def test_sentiment_exception_handled(self):
        """情感分析异常不影响主流程"""
        from agents.scenarios.stock_analysis import handle_stock_analysis
        with patch("agents.scenarios.common.resolve_stock", return_value=("600519", "茅台")), \
             patch("agents.scenarios.common.fetch_stock_bundle", new_callable=AsyncMock,
                   return_value=_make_bundle()), \
             patch("agents.scenarios.common.BaseScenarioHandler.calc_tech_indicators",
                   return_value={}), \
             patch("tools.sentiment.analyze_sentiment",
                   side_effect=RuntimeError("分析失败")), \
             patch("agents.scenarios.common.BaseScenarioHandler.call_llm",
                   new_callable=AsyncMock, return_value={}):
            result = await handle_stock_analysis("分析茅台", "分析茅台", {}, "2026-01-01")
        assert result is not None


class TestFormatOutput:
    """_format_output: 格式化输出"""

    def test_with_all_data(self):
        from agents.scenarios.stock_analysis import _format_output
        data = _make_bundle()
        tech = {"macd": 0.5, "rsi_14": 60}
        sentiment = {"total_score": 0.8, "conclusion": "正面"}
        analysis = {
            "tech_analysis": "MACD金叉", "news_summary": "利好",
            "fundamental": "PE合理", "conclusion_short": "看涨",
            "conclusion_mid": "中性", "risk": "低"
        }
        result = _format_output("茅台", "600519", data, tech, sentiment, analysis, "2026-01-01")
        assert "茅台" in result
        assert "600519" in result
        assert "MACD" in result

    def test_empty_analysis(self):
        from agents.scenarios.stock_analysis import _format_output
        data = _make_bundle()
        result = _format_output("茅台", "600519", data, {}, {}, {}, "2026-01-01")
        assert "茅台" in result

    def test_no_realtime_data(self):
        from agents.scenarios.stock_analysis import _format_output
        data = {"realtime": {}, "news": [], "rating": {}}
        result = _format_output("茅台", "600519", data, {}, {}, {}, "2026-01-01")
        assert "茅台" in result

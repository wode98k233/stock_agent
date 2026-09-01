"""
测试目标: agents/scenarios/comparison.py
覆盖范围:
  - _build_metrics: 指标构建（PE/PB/MACD/市值分支）
  - handle_comparison: 股票不足、异常过滤、正常流程
Mock 策略: mock resolve_stocks、fetch_stock_bundle、BaseScenarioHandler
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── _build_metrics ───────────────────────────────────────────

class TestBuildMetrics:
    """_build_metrics: 对比指标构建"""

    def _make_valid(self, pe=30, pb=10, macd=0.5, mv=200000, turnover=1.5, rsi=60):
        stock = {"code": "600519", "name": "茅台"}
        bundle = {"realtime": {"price": 1800, "pct_chg": 2.5, "pe": pe, "pb": pb,
                                "turnover_rate": turnover, "total_mv": mv}}
        tech = {"macd": macd, "rsi_14": rsi}
        return [(stock, bundle, tech)]

    def test_single_stock(self):
        from agents.scenarios.comparison import _build_metrics
        result = _build_metrics(self._make_valid())
        assert len(result) > 0
        labels = [m["label"] for m in result]
        assert "现价" in labels
        assert "今日涨幅" in labels

    def test_pe_positive(self):
        from agents.scenarios.comparison import _build_metrics
        result = _build_metrics(self._make_valid(pe=30))
        pe_metric = next(m for m in result if m["label"] == "PE(动态)")
        assert pe_metric["values"] == ["30.0"]

    def test_pe_zero_shows_dash(self):
        from agents.scenarios.comparison import _build_metrics
        result = _build_metrics(self._make_valid(pe=0))
        pe_metric = next(m for m in result if m["label"] == "PE(动态)")
        assert pe_metric["values"] == ["-"]

    def test_macd_positive_golden_cross(self):
        from agents.scenarios.comparison import _build_metrics
        result = _build_metrics(self._make_valid(macd=0.5))
        macd_metric = next(m for m in result if m["label"] == "MACD")
        assert "金叉" in macd_metric["values"][0]

    def test_macd_negative_death_cross(self):
        from agents.scenarios.comparison import _build_metrics
        result = _build_metrics(self._make_valid(macd=-0.3))
        macd_metric = next(m for m in result if m["label"] == "MACD")
        assert "死叉" in macd_metric["values"][0]

    def test_large_market_cap_formatted(self):
        from agents.scenarios.comparison import _build_metrics
        result = _build_metrics(self._make_valid(mv=200000))
        mc_metric = next(m for m in result if m["label"] == "总市值")
        # mv > 10000 → 格式化
        assert mc_metric["values"][0] != "200000"


# ── handle_comparison ────────────────────────────────────────

class TestHandleComparison:
    """handle_comparison: 完整流程"""

    @pytest.mark.asyncio
    async def test_less_than_2_stocks_returns_none(self):
        from agents.scenarios.comparison import handle_comparison
        with patch("agents.scenarios.comparison.resolve_stocks", return_value=[{"code": "600519", "name": "茅台"}]):
            result = await handle_comparison("分析茅台", "", {}, "2026-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_all_bundles_fail_returns_none(self):
        from agents.scenarios.comparison import handle_comparison
        stocks = [{"code": "600519", "name": "茅台"}, {"code": "000858", "name": "五粮液"}]
        with patch("agents.scenarios.comparison.resolve_stocks", return_value=stocks), \
             patch("agents.scenarios.comparison.fetch_stock_bundle", new_callable=AsyncMock,
                   side_effect=RuntimeError("网络错误")):
            result = await handle_comparison("对比", "", {}, "2026-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_partial_failure_with_enough_valid(self):
        from agents.scenarios.comparison import handle_comparison
        stocks = [{"code": "600519", "name": "茅台"}, {"code": "000858", "name": "五粮液"},
                  {"code": "002304", "name": "洋河"}]
        bundle_ok = {"realtime": {"price": 1800, "pct_chg": 2.5, "pe": 30, "pb": 10,
                                   "turnover_rate": 1.5, "total_mv": 200000}}
        call_count = 0
        async def mock_fetch(code):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("失败")
            return bundle_ok

        with patch("agents.scenarios.comparison.resolve_stocks", return_value=stocks), \
             patch("agents.scenarios.comparison.fetch_stock_bundle", side_effect=mock_fetch), \
             patch("agents.scenarios.common.BaseScenarioHandler.calc_tech_indicators", return_value={}), \
             patch("agents.scenarios.common.BaseScenarioHandler.call_llm", new_callable=AsyncMock, return_value="结论"):
            result = await handle_comparison("对比", "", {}, "2026-01-01")
        assert result is not None

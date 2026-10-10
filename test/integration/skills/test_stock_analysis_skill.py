"""个股分析技能（stock_analysis）集成测试

覆盖由 stock_query + technical_analysis + risk_metrics 合并而来的 10 个工具：
- 基础数据：get_stock_realtime / get_stock_history / get_industry_list / get_concept_list
- 技术指标：calc_technical_indicators
- 风险指标：get_risk_metrics

这些工具经 `tools/fetcher` 多源降级链取数（`mx_data → sina_direct → hithink → akshare`），
因此妙想额度耗尽时仍可返回数据。需要网络连接。
"""
import importlib.util
import json
import logging
import os

import pytest

# tools.skills.stock_analysis 不能直接 import（tools.skills.py 和 tools/skills/ 冲突）
_SKILL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tools", "skills", "stock_analysis", "main.py")
_SKILL_PATH = os.path.normpath(_SKILL_PATH)
_spec = importlib.util.spec_from_file_location("stock_analysis_main", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
StockAnalysisSkill = _mod.StockAnalysisSkill

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(60),
]


@pytest.fixture
def skill():
    """创建 StockAnalysisSkill 实例"""
    logger = logging.getLogger("test_stock_analysis")
    return StockAnalysisSkill(logger)


def _parse(result):
    """skill_tool 装饰器返回 JSON 字符串，解析为 Python 对象"""
    if isinstance(result, str):
        return json.loads(result)
    return result


# ── get_stock_realtime ──────────────────────────────────────

class TestStockRealtime:
    """实时行情"""

    def test_returns_data(self, skill, test_stock):
        raw = skill.get_stock_realtime(test_stock)
        result = _parse(raw)
        assert isinstance(result, dict)

    def test_has_price(self, skill, test_stock):
        raw = skill.get_stock_realtime(test_stock)
        result = _parse(raw)
        assert len(result) > 0

    def test_nonexistent_stock(self, skill):
        """不存在的股票返回错误信息（不抛异常）"""
        raw = skill.get_stock_realtime("999999")
        assert raw is not None
        assert len(raw) > 0


# ── get_stock_history ───────────────────────────────────────

class TestStockHistory:
    """历史 K 线"""

    def test_returns_dict_with_data(self, skill, test_stock):
        raw = skill.get_stock_history(test_stock, days=30)
        result = _parse(raw)
        assert isinstance(result, dict)
        assert "total_days" in result
        assert result["total_days"] > 0

    def test_has_latest_5(self, skill, test_stock):
        raw = skill.get_stock_history(test_stock, days=30)
        result = _parse(raw)
        assert "latest_5" in result
        assert isinstance(result["latest_5"], list)


# ── 板块列表 ────────────────────────────────────────────────

class TestIndustryList:
    """行业板块列表"""

    def test_returns_list(self, skill):
        result = _parse(skill.get_industry_list())
        assert isinstance(result, list)
        assert len(result) > 0


class TestConceptList:
    """概念板块列表"""

    def test_returns_list(self, skill):
        result = _parse(skill.get_concept_list())
        assert isinstance(result, list)
        assert len(result) > 0


# ── calc_technical_indicators（原 technical_analysis）────────

class TestTechnicalIndicators:
    """技术指标：合并进本技能后必须仍可计算（AI 评审点名的能力）"""

    def test_returns_indicators(self, skill, test_stock):
        result = _parse(skill.calc_technical_indicators(test_stock))
        assert isinstance(result, dict)
        assert "code" in result
        # 关键指标必须给出数值，而不是「数据缺失」
        for key in ("rsi", "dif", "dea", "macd_bar", "ma5", "ma10", "ma20"):
            assert key in result, f"缺少技术指标字段 {key}"

    def test_ma_and_macd_are_numeric(self, skill, test_stock):
        result = _parse(skill.calc_technical_indicators(test_stock))
        assert isinstance(result["rsi"], (int, float))
        assert isinstance(result["dif"], (int, float))
        assert isinstance(result["ma20"], (int, float))

    def test_history_unavailable_returns_structured_fallback(self, skill):
        """历史K线不可用时返回 retry:false 的结构化降级，而不是抛异常"""
        result = _parse(skill.calc_technical_indicators("999999"))
        assert isinstance(result, dict)
        assert result.get("retry") is False
        assert "error" in result


# ── get_risk_metrics（原 risk_metrics）──────────────────────

class TestRiskMetrics:
    """风险指标：合并进本技能后必须仍可计算"""

    def test_returns_risk_metrics(self, skill, test_stock):
        result = _parse(skill.get_risk_metrics(test_stock, 120, "000300"))
        assert isinstance(result, dict)
        assert result.get("symbol") == test_stock
        for key in ("volatility", "max_drawdown", "sharpe_ratio"):
            assert key in result, f"缺少风险指标字段 {key}"

    def test_benchmark_recorded(self, skill, test_stock):
        result = _parse(skill.get_risk_metrics(test_stock, 120, "000300"))
        assert "benchmark" in result

"""
记忆系统 — ConfidenceCalculator 单元测试

覆盖：4 因子计算、来源推断、clamp 边界、各分支。
"""
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta

from memory.backend.base import MemoryEntry
from memory.confidence import ConfidenceCalculator, get_confidence_calculator


class TestSourceInference:
    """来源类型推断"""

    def test_tool_hard_data_wins(self):
        """工具硬数据（PE/PB 等数值字段）→ tool_hard_data"""
        extracts = [{"pe": 30.5, "roe": 15.2}]
        assert ConfidenceCalculator.infer_source(extracts=extracts) == "tool_hard_data"

    def test_dashboard_llm_when_no_tool_data(self):
        """有仪表盘但无工具硬数据 → dashboard_llm"""
        dash_meta = {"key_findings": ["估值偏低"], "sentiment": "bullish"}
        assert ConfidenceCalculator.infer_source(dash_meta=dash_meta) == "dashboard_llm"

    def test_report_fallback_when_extracts_no_hard_data(self):
        """extracts 有内容但无硬数据 → report_fallback"""
        extracts = [{"stock_code": "000001"}]
        assert ConfidenceCalculator.infer_source(extracts=extracts) == "report_fallback"

    def test_llm_speculation_when_nothing(self):
        """完全无数据 → llm_speculation"""
        assert ConfidenceCalculator.infer_source() == "llm_speculation"

    def test_tool_change_pct_counts_as_hard_data(self):
        """涨跌幅也是硬数据"""
        extracts = [{"change_pct": 9.18}]
        assert ConfidenceCalculator.infer_source(extracts=extracts) == "tool_hard_data"

    def test_tool_turnover_rate_counts_as_hard_data(self):
        """换手率也是硬数据"""
        extracts = [{"turnover_rate": 5.2}]
        assert ConfidenceCalculator.infer_source(extracts=extracts) == "tool_hard_data"


class TestBaseScore:
    """基础置信度映射"""

    @pytest.fixture
    def calc(self):
        return ConfidenceCalculator()

    def test_tool_hard_data_base(self, calc):
        assert calc.compute_base("tool_hard_data") == 0.9

    def test_dashboard_llm_base(self, calc):
        assert calc.compute_base("dashboard_llm") == 0.7

    def test_report_fallback_base(self, calc):
        assert calc.compute_base("report_fallback") == 0.5

    def test_llm_speculation_base(self, calc):
        assert calc.compute_base("llm_speculation") == 0.3

    def test_unknown_source_defaults_to_0_5(self, calc):
        assert calc.compute_base("unknown_source") == 0.5


class TestFreshness:
    """新鲜度衰减"""

    @pytest.fixture
    def calc(self):
        return ConfidenceCalculator()

    def test_today_is_1_0(self, calc):
        today = datetime.now().strftime("%Y-%m-%d")
        assert calc.compute_freshness(today) == 1.0

    def test_seven_days_is_1_0(self, calc):
        seven_days = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        assert calc.compute_freshness(seven_days) == 1.0

    def test_thirty_days_is_min(self, calc):
        thirty_days = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        assert calc.compute_freshness(thirty_days) == 0.5

    def test_midpoint_decay(self, calc):
        """18.5 天（7~30 中点）≈ 0.75"""
        days = 7 + (30 - 7) // 2  # ~18.5 days
        date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        f = calc.compute_freshness(date)
        assert 0.7 < f < 0.8  # 大约 0.75

    def test_iso_datetime_parsed(self, calc):
        """ISO 时间戳格式正确截取"""
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        assert calc.compute_freshness(ts) == 1.0

    def test_invalid_date_defaults_to_0_7(self, calc):
        assert calc.compute_freshness("not_a_date") == 0.7

    def test_none_date_defaults_to_0_7(self, calc):
        assert calc.compute_freshness(None) == 0.7


class TestFeedback:
    """用户反馈系数"""

    @pytest.fixture
    def calc(self):
        return ConfidenceCalculator()

    def test_good_is_1_1(self, calc):
        assert calc.compute_feedback({"action": "good"}) == 1.1

    def test_bad_is_0_7(self, calc):
        assert calc.compute_feedback({"action": "bad"}) == 0.7

    def test_neutral_is_1_0(self, calc):
        assert calc.compute_feedback({"action": "neutral"}) == 1.0

    def test_no_feedback_is_1_0(self, calc):
        assert calc.compute_feedback(None) == 1.0

    def test_empty_dict_is_1_0(self, calc):
        assert calc.compute_feedback({}) == 1.0


class TestCorroboration:
    """多源印证"""

    @pytest.fixture
    def calc(self):
        return ConfidenceCalculator()

    def test_single_conclusion_is_1_0(self, calc):
        recent = [{"stock_code": "000001", "sentiment": "bullish"}]
        assert calc.compute_corroboration("000001", "bullish", recent) == 1.0

    def test_two_same_conclusions_is_1_05(self, calc):
        recent = [
            {"stock_code": "000001", "sentiment": "bullish"},
            {"stock_code": "000001", "sentiment": "bullish"},
        ]
        assert calc.compute_corroboration("000001", "bullish", recent) == 1.05

    def test_three_is_1_10(self, calc):
        recent = [
            {"stock_code": "000001", "sentiment": "bullish"},
            {"stock_code": "000001", "sentiment": "bullish"},
            {"stock_code": "000001", "sentiment": "bullish"},
        ]
        assert calc.compute_corroboration("000001", "bullish", recent) == 1.10

    def test_max_extra_is_4(self, calc):
        """5 个同结论 → 1.0 + 0.05 × 4 = 1.20"""
        recent = [{"stock_code": "000001", "sentiment": "bullish"}] * 6
        assert calc.compute_corroboration("000001", "bullish", recent) == 1.20

    def test_different_stock_not_counted(self, calc):
        recent = [
            {"stock_code": "000002", "sentiment": "bullish"},
            {"stock_code": "000002", "sentiment": "bullish"},
        ]
        assert calc.compute_corroboration("000001", "bullish", recent) == 1.0

    def test_different_sentiment_not_counted(self, calc):
        recent = [
            {"stock_code": "000001", "sentiment": "bearish"},
        ]
        assert calc.compute_corroboration("000001", "bullish", recent) == 1.0

    def test_none_recent_is_1_0(self, calc):
        assert calc.compute_corroboration("000001", "bullish", None) == 1.0

    def test_empty_stock_code_is_1_0(self, calc):
        recent = [{"stock_code": "000001", "sentiment": "bullish"}]
        assert calc.compute_corroboration("", "bullish", recent) == 1.0


class TestCompute:
    """完整合成计算"""

    @pytest.fixture
    def calc(self):
        return ConfidenceCalculator()

    def test_full_synthesis(self, calc):
        """base=0.9 × freshness=1.0 × feedback=1.0 × corroboration=1.0 = 0.9"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="test", metadata={"date": datetime.now().strftime("%Y-%m-%d")},
        )
        result = calc.compute(entry=entry, source="tool_hard_data")
        assert result["confidence"] == 0.9
        assert result["factors"]["base"] == 0.9

    def test_clamp_to_1_0(self, calc):
        """freshness=1.0 × good feedback=1.1 × 大量印证... clamp 到 1.0"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="test", metadata={
                "date": datetime.now().strftime("%Y-%m-%d"),
                "sentiment": "bullish",
            },
        )
        recent = [{"stock_code": "000001", "sentiment": "bullish"}] * 10  # 印证 ×1.20
        result = calc.compute(
            entry=entry, source="tool_hard_data",
            feedback_history={"action": "good"},
            recent_conclusions=recent,
        )
        # 0.9 × 1.0 × 1.1 × 1.20 = 1.188 → clamp to 1.0
        assert result["confidence"] == 1.0

    def test_clamp_to_0_0(self, calc):
        """低基+旧+差评 → 不能低于 0"""
        old_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="test", metadata={"date": old_date},
        )
        result = calc.compute(
            entry=entry, source="llm_speculation",
            feedback_history={"action": "bad"},
        )
        assert result["confidence"] >= 0.0

    def test_no_entry_returns_default(self, calc):
        result = calc.compute(entry=None)
        assert result["confidence"] == 0.5
        assert result["factors"]["base"] == 0.5

    def test_auto_infer_source(self, calc):
        """不指定 source 时自动从 extracts 推断"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="test", metadata={"date": datetime.now().strftime("%Y-%m-%d")},
        )
        extracts = [{"pe": 30.5}]
        result = calc.compute(entry=entry, extracts=extracts)
        assert result["factors"]["source"] == "tool_hard_data"
        assert result["factors"]["base"] == 0.9


class TestSingleton:
    """模块级单例"""

    def test_get_calculator_returns_same_instance(self):
        c1 = get_confidence_calculator()
        c2 = get_confidence_calculator()
        assert c1 is c2

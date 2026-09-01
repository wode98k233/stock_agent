"""
插槽提取器单元测试

每个核心插槽至少 3 种输入样例。
运行: pytest test/unit/analysis/test_extractors.py -v
"""
import os
import sys
import json
import pytest
from unittest.mock import MagicMock, patch

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _make_bag(**overrides):
    """构建测试用 EvidenceBag"""
    bag = {
        "items": [],
        "raw_text": "",
        "news_text": "",
        "json_fragments": [],
        "normalized_fields": {},
        "table_fragments": [],
        "metadata": {},
    }
    bag.update(overrides)
    return bag


# ══════════════════════════════════════════════════════════════
# rsi_status 提取器
# ══════════════════════════════════════════════════════════════

def test_rsi_from_normalized_fields():
    """从 normalized_fields 提取 RSI"""
    from agents.analysis.extractors import extract_rsi_status
    bag = _make_bag(normalized_fields={"rsi": 65.3})
    result = extract_rsi_status(bag, {})
    assert result is not None
    assert result.value == 65.3
    assert result.status == "中性"  # 65.3 在 30-70 区间
    assert result.method == "json"


def test_rsi_from_json_fragment():
    """从 json_fragments 提取 RSI"""
    from agents.analysis.extractors import extract_rsi_status
    bag = _make_bag(json_fragments=[{"tool": "tech", "data": {"RSI": 82.0}}])
    result = extract_rsi_status(bag, {})
    assert result is not None
    assert result.status == "超买"


def test_rsi_from_text_regex():
    """从文本正则提取 RSI"""
    from agents.analysis.extractors import extract_rsi_status
    bag = _make_bag(raw_text="RSI: 25.5，处于超卖区域")
    result = extract_rsi_status(bag, {})
    assert result is not None
    assert result.status == "偏弱"
    assert result.method == "regex"


def test_rsi_missing():
    """无 RSI 数据 → None"""
    from agents.analysis.extractors import extract_rsi_status
    bag = _make_bag()
    result = extract_rsi_status(bag, {})
    assert result is None


def test_rsi_interpretation_boundary():
    """RSI 边界值测试"""
    from agents.analysis.extractors import _interpret_rsi

    r = _interpret_rsi(80.0, "test", "json")
    assert r.status == "超买"

    r = _interpret_rsi(70.0, "test", "json")
    assert r.status == "偏强"

    r = _interpret_rsi(50.0, "test", "json")
    assert r.status == "中性"

    r = _interpret_rsi(30.0, "test", "json")
    assert r.status == "偏弱"

    r = _interpret_rsi(20.0, "test", "json")
    assert r.status == "超卖"


# ══════════════════════════════════════════════════════════════
# macd_signal 提取器
# ══════════════════════════════════════════════════════════════

def test_macd_from_normalized_fields():
    """从 normalized_fields 提取 MACD（金叉）"""
    from agents.analysis.extractors import extract_macd_signal
    bag = _make_bag(normalized_fields={"DIF": 0.05, "DEA": 0.03, "MACD": 0.02})
    result = extract_macd_signal(bag, {})
    assert result is not None
    assert result.status == "金叉"


def test_macd_death_cross():
    """MACD 死叉"""
    from agents.analysis.extractors import extract_macd_signal
    bag = _make_bag(normalized_fields={"DIF": -0.02, "DEA": 0.01, "MACD": -0.03})
    result = extract_macd_signal(bag, {})
    assert result is not None
    assert result.status == "死叉"


def test_macd_from_text():
    """从文本提取 MACD"""
    from agents.analysis.extractors import extract_macd_signal
    bag = _make_bag(raw_text="MACD: 0.015，金叉信号")
    result = extract_macd_signal(bag, {})
    assert result is not None
    assert result.method == "regex"


def test_macd_missing():
    """无 MACD 数据 → None"""
    from agents.analysis.extractors import extract_macd_signal
    bag = _make_bag()
    result = extract_macd_signal(bag, {})
    assert result is None


# ══════════════════════════════════════════════════════════════
# pe_percentile 提取器
# ══════════════════════════════════════════════════════════════

def test_pe_from_normalized_fields():
    """从 normalized_fields 提取 PE 分位"""
    from agents.analysis.extractors import extract_pe_percentile
    bag = _make_bag(normalized_fields={"pe_percentile": 72, "pe": 32.5})
    result = extract_pe_percentile(bag, {})
    assert result is not None
    assert result.status == "偏高"
    assert result.method == "json"


def test_pe_low_valuation():
    """PE 低估"""
    from agents.analysis.extractors import extract_pe_percentile
    bag = _make_bag(normalized_fields={"pe_percentile": 15, "pe": 12.0})
    result = extract_pe_percentile(bag, {})
    assert result is not None
    assert result.status == "低估"


def test_pe_no_percentile():
    """有 PE 值但无分位"""
    from agents.analysis.extractors import extract_pe_percentile
    bag = _make_bag(normalized_fields={"pe": 25.0})
    result = extract_pe_percentile(bag, {})
    assert result is not None
    assert result.status == "无分位"


def test_pe_from_text():
    """从文本提取 PE"""
    from agents.analysis.extractors import extract_pe_percentile
    bag = _make_bag(raw_text="PE: 28.5，市盈率合理")
    result = extract_pe_percentile(bag, {})
    assert result is not None
    assert result.method == "regex"


def test_pe_missing():
    """无 PE 数据 → None"""
    from agents.analysis.extractors import extract_pe_percentile
    bag = _make_bag()
    result = extract_pe_percentile(bag, {})
    assert result is None


# ══════════════════════════════════════════════════════════════
# trend 提取器
# ══════════════════════════════════════════════════════════════

def test_trend_strong_up():
    """强势上涨"""
    from agents.analysis.extractors import extract_trend
    bag = _make_bag(normalized_fields={"涨跌幅": 5.2})
    result = extract_trend(bag, {})
    assert result is not None
    assert result.status == "强势上涨"


def test_trend_mild_up():
    """温和上涨"""
    from agents.analysis.extractors import extract_trend
    bag = _make_bag(normalized_fields={"涨跌幅": 1.5})
    result = extract_trend(bag, {})
    assert result is not None
    assert result.status == "温和上涨"


def test_trend_mild_down():
    """温和下跌"""
    from agents.analysis.extractors import extract_trend
    bag = _make_bag(normalized_fields={"涨跌幅": -1.2})
    result = extract_trend(bag, {})
    assert result is not None
    assert result.status == "温和下跌"


def test_trend_strong_down():
    """大幅下跌"""
    from agents.analysis.extractors import extract_trend
    bag = _make_bag(normalized_fields={"涨跌幅": -5.0})
    result = extract_trend(bag, {})
    assert result is not None
    assert result.status == "大幅下跌"


def test_trend_from_text():
    """从文本提取趋势"""
    from agents.analysis.extractors import extract_trend
    bag = _make_bag(raw_text="今日涨停，成交量放大")
    result = extract_trend(bag, {})
    assert result is not None
    assert result.status == "涨停"


def test_trend_missing():
    """无趋势数据 → None"""
    from agents.analysis.extractors import extract_trend
    bag = _make_bag()
    result = extract_trend(bag, {})
    assert result is None


# ══════════════════════════════════════════════════════════════
# news_sentiment 提取器
# ══════════════════════════════════════════════════════════════

def test_news_sentiment_no_news():
    """无新闻 → None"""
    from agents.analysis.extractors import extract_news_sentiment
    bag = _make_bag(news_text="")
    result = extract_news_sentiment(bag, {})
    assert result is None


def test_news_sentiment_short_text():
    """新闻太短 → None"""
    from agents.analysis.extractors import extract_news_sentiment
    bag = _make_bag(news_text="短")
    result = extract_news_sentiment(bag, {})
    assert result is None


def test_news_sentiment_budget_exceeded():
    """预算不足 → None"""
    from agents.analysis.extractors import extract_news_sentiment
    bag = _make_bag(news_text="这是一条足够长的新闻内容，用于测试预算不足的情况")
    budget = MagicMock()
    budget.get_status.return_value = {"calls_percent": 95}
    result = extract_news_sentiment(bag, {"budget": budget})
    assert result is None


# ══════════════════════════════════════════════════════════════
# 派生插槽提取器
# ══════════════════════════════════════════════════════════════

def test_verdict_derived_from_existing_signals():
    """核心结论从趋势、MACD、RSI 等已有信号派生，不额外调用 LLM"""
    from agents.analysis.extractors import extract_all

    bag = _make_bag(
        normalized_fields={"涨跌幅": 3.8, "DIF": 0.05, "DEA": 0.03, "MACD": 0.02, "RSI": 72},
    )

    results = extract_all(bag, ["trend", "macd_signal", "rsi_status", "verdict"], {})

    assert "verdict" in results
    assert results["verdict"].method == "derived"
    assert results["verdict"].status in ("偏多", "谨慎偏多")
    assert "趋势" in results["verdict"].interpretation


def test_signal_strength_derived_from_existing_signals():
    """信号强度能根据已填充插槽给出强/中/弱判断"""
    from agents.analysis.extractors import extract_all

    bag = _make_bag(
        normalized_fields={"涨跌幅": -4.2, "DIF": -0.05, "DEA": 0.01, "MACD": -0.06, "RSI": 22},
    )

    results = extract_all(bag, ["trend", "macd_signal", "rsi_status", "signal_strength"], {})

    assert "signal_strength" in results
    assert results["signal_strength"].method == "derived"
    assert results["signal_strength"].status in ("强", "中")
    assert "有效信号" in results["signal_strength"].interpretation


def test_market_breadth_from_scenario_adapter_fields():
    """市场广度可读取 scenario_adapter 生成的 breadth_* 字段"""
    from agents.analysis.extractors import extract_market_breadth

    bag = _make_bag(
        normalized_fields={
            "breadth_up": 3260,
            "breadth_down": 1700,
            "breadth_flat": 180,
            "breadth_limit_up": 65,
            "breadth_limit_down": 12,
        },
    )

    result = extract_market_breadth(bag, {})

    assert result is not None
    assert result.method == "json"
    assert result.value["up"] == 3260
    assert result.value["down"] == 1700
    assert result.status == "偏暖"


# ══════════════════════════════════════════════════════════════
# extract_all
# ══════════════════════════════════════════════════════════════

def test_extract_all_multiple_slots():
    """extract_all 提取多个插槽"""
    from agents.analysis.extractors import extract_all
    bag = _make_bag(
        normalized_fields={"rsi": 55.0, "DIF": 0.02, "DEA": 0.01, "MACD": 0.01},
    )
    results = extract_all(bag, ["rsi_status", "macd_signal", "trend"], {})
    assert "rsi_status" in results
    assert "macd_signal" in results


def test_extract_all_unknown_slot():
    """未知插槽名 → 跳过"""
    from agents.analysis.extractors import extract_all
    bag = _make_bag()
    results = extract_all(bag, ["nonexistent_slot"], {})
    assert "nonexistent_slot" not in results


# ══════════════════════════════════════════════════════════════
# decorator registry
# ══════════════════════════════════════════════════════════════

def test_extractor_registry():
    """提取器注册表正确注册"""
    from agents.analysis.extractors import _EXTRACTORS
    expected = [
        "rsi_status", "macd_signal", "pe_percentile", "trend", "news_sentiment",
        "verdict", "signal_strength", "market_breadth",
    ]
    for name in expected:
        assert name in _EXTRACTORS, f"提取器 {name} 未注册"


if __name__ == "__main__":
    import traceback
    tests = [
        test_rsi_from_normalized_fields, test_rsi_from_json_fragment,
        test_rsi_from_text_regex, test_rsi_missing, test_rsi_interpretation_boundary,
        test_macd_from_normalized_fields, test_macd_death_cross,
        test_macd_from_text, test_macd_missing,
        test_pe_from_normalized_fields, test_pe_low_valuation,
        test_pe_no_percentile, test_pe_from_text, test_pe_missing,
        test_trend_strong_up, test_trend_mild_up, test_trend_mild_down,
        test_trend_strong_down, test_trend_from_text, test_trend_missing,
        test_news_sentiment_no_news, test_news_sentiment_short_text,
        test_news_sentiment_budget_exceeded,
        test_extract_all_multiple_slots, test_extract_all_unknown_slot,
        test_extractor_registry,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed > 0 else 0)

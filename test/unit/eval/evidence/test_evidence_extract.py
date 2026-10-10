"""抽取与归一化单测。"""
from eval.evidence.extract import (
    NumberToken,
    contains_quote,
    extract_dates,
    extract_numbers,
    extract_stock_codes,
    latest_date,
    normalize_text,
    numbers_match,
)


# ── 归一化 ────────────────────────────────────────────────────────────
def test_normalize_strips_markdown_and_punct():
    assert normalize_text("**PE 约 35 倍**") == "PE 约 35 倍"
    assert normalize_text("`code`") == "code"
    assert normalize_text("营收，同比：+12.3%") == "营收,同比:+12.3%"


def test_normalize_collapses_whitespace():
    assert normalize_text("a\n\n   b\t c") == "a b c"
    assert normalize_text(None) == ""


def test_normalize_keeps_number_forms_intact():
    """归一化不改数字本身：千分位与单位保留，交给 numbers_match 处理。"""
    assert normalize_text("1,234.5") == "1,234.5"
    assert normalize_text("12.3%") == "12.3%"


def test_contains_quote_hits_across_formatting():
    haystack = "综合来看**该股**上行空间明确，\n建议逢低布局"
    assert contains_quote(haystack, "该股上行空间明确")
    assert contains_quote(haystack, "建议逢低布局")
    assert not contains_quote(haystack, "该股下行风险明确")


def test_contains_quote_empty_safe():
    assert not contains_quote("", "x")
    assert not contains_quote("x", "")


# ── 数字抽取 ──────────────────────────────────────────────────────────
def test_extract_basic_numbers():
    toks = extract_numbers("营收同比 +12.3%，净利同比 +8.1%")
    vals = [t.value for t in toks]
    assert 12.3 in vals and 8.1 in vals
    assert all(t.unit == "%" for t in toks)


def test_extract_thousands_separator():
    toks = extract_numbers("成交额 1,234,567 元")
    assert any(t.value == 1234567.0 for t in toks)


def test_extract_multiplier_units():
    cases = {"1.2万": 12000.0, "3亿": 3e8, "0.5万亿": 5e11, "2500万": 2.5e7}
    for text, expect in cases.items():
        toks = extract_numbers(text)
        assert toks, text
        assert toks[0].value == expect, f"{text} → {toks[0].value}"


def test_extract_negative():
    toks = extract_numbers("回撤 -30.19%")
    assert any(t.value == -30.19 for t in toks)


def test_extract_skips_stock_codes_by_default():
    """6 位纯数字默认当股票代码跳过，避免制造噪音。"""
    toks = extract_numbers("600519 当前价 1680.5 元")
    assert not any(t.raw == "600519" for t in toks)
    assert any(t.value == 1680.5 for t in toks)
    # 显式要求时不跳过
    toks2 = extract_numbers("600519", skip_stock_codes=False)
    assert any(t.value == 600519.0 for t in toks2)


def test_extract_keeps_6digit_with_unit():
    """带单位/小数的 6 位数不是代码，要保留。"""
    toks = extract_numbers("123456.7")
    assert any(t.value == 123456.7 for t in toks)


def test_extract_context_window():
    t = extract_numbers("今天上证指数收于 3210.5 点，成交额放大")[0]
    assert "3210.5" in t.context
    assert "成交额" in t.context


def test_extract_numbers_empty_safe():
    assert extract_numbers("") == []
    assert extract_numbers(None) == []


def test_number_token_to_dict():
    t = NumberToken(raw="12.3%", value=12.3, unit="%", span=(0, 5), context="x")
    assert t.is_percent
    assert t.to_dict()["unit"] == "%"


# ── 数字匹配容忍度 ────────────────────────────────────────────────────
def test_match_exact_and_rounding():
    assert numbers_match(28.4, 28.4)
    assert numbers_match(28.4, 28.43)        # 四舍五入
    assert numbers_match(1680.5, 1680.5)


def test_match_rejects_different_values():
    assert not numbers_match(35.0, 28.4)     # PE 35 vs 28.4 必须判为不同
    assert not numbers_match(12.3, 9.8)


def test_match_percent_ratio_equivalence():
    """0.123 ↔ 12.3% 应视为同一个值（比值/百分数混用）。"""
    pct = extract_numbers("12.3%")[0]
    assert numbers_match(pct, 0.123)
    assert numbers_match(pct, 12.3)


def test_match_accepts_tokens_and_floats():
    tok = extract_numbers("3亿")[0]
    assert numbers_match(tok, 3e8)
    assert not numbers_match(tok, 3e7)


def test_match_zero_edges():
    assert numbers_match(0.0, 0.0)
    assert not numbers_match(0.0, 1.0)


# ── 股票代码 ──────────────────────────────────────────────────────────
def test_extract_stock_codes_dedup_and_order():
    codes = extract_stock_codes("600519 和 000001，再看 600519")
    assert codes == ["600519", "000001"]


def test_extract_stock_codes_ignores_embedded():
    assert extract_stock_codes("1234567") == []
    assert extract_stock_codes("") == []


# ── 日期 ──────────────────────────────────────────────────────────────
def test_extract_dates_multiple_formats():
    assert extract_dates("2026-06-05") == ["2026-06-05"]
    assert extract_dates("2026/6/5") == ["2026-06-05"]
    assert extract_dates("2026年6月5日") == ["2026-06-05"]


def test_extract_dates_year_month_only():
    assert extract_dates("2026年6月") == ["2026-06-01"]


def test_extract_dates_rejects_illegal():
    assert extract_dates("2026-13-45") == []


def test_latest_date():
    assert latest_date("从 2026-06-05 到 2026-09-18") == "2026-09-18"
    assert latest_date("无日期") is None
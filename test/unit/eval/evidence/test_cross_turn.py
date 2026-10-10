"""跨轮取证单测：重复调用 / 跨轮数字矛盾 / 否定约束（含"无问题"负例）。

设计文档：docs/superpowers/specs/2026-09-18-eval-redesign-design.md §9/§18
"""
from eval.evidence.cross_turn import (
    detect_constraint_violations,
    detect_number_conflicts,
    detect_repeat_calls,
    normalize_args,
    summarize_cross_turn,
)


def _turn(turn_index, question="", answer="", tool_calls=None):
    return {"turn_index": turn_index, "question": question,
            "answer": answer, "tool_calls": tool_calls or []}


def _tc(name, args, step_ref=None):
    return {"name": name, "args": args, "step_ref": step_ref}


# ── 重复调用 ─────────────────────────────────────────────────────────

def test_repeat_calls_detected_across_turns():
    turns = [
        _turn(1, tool_calls=[_tc("ak_stock_zh_a_hist", '{"code":"600519"}', 11)]),
        _turn(2, tool_calls=[_tc("ak_stock_zh_a_hist", '{"code":"600519"}', 25)]),
    ]
    reps = detect_repeat_calls(turns)
    assert len(reps) == 1
    assert reps[0]["tool"] == "ak_stock_zh_a_hist"
    assert reps[0]["count"] == 2
    assert [o["step_ref"] for o in reps[0]["occurrences"]] == [11, 25]
    assert [o["turn_index"] for o in reps[0]["occurrences"]] == [1, 2]


def test_repeat_calls_args_normalization_ignores_key_order():
    """JSON 参数键序不同、空白差异 → 同一指纹，仍判重复。"""
    turns = [
        _turn(1, tool_calls=[_tc("t", '{"a": 1, "b": 2}', 1)]),
        _turn(2, tool_calls=[_tc("t", '{"b":2,"a":1}', 2)]),
    ]
    reps = detect_repeat_calls(turns)
    assert len(reps) == 1 and reps[0]["count"] == 2


def test_repeat_calls_no_repeat_negative():
    """跨轮无重复（参数不同）→ 空结果（负例）。"""
    turns = [
        _turn(1, tool_calls=[_tc("t", '{"code":"600519"}', 1)]),
        _turn(2, tool_calls=[_tc("t", '{"code":"000001"}', 2),
                             _tc("other", '{"code":"600519"}', 3)]),
    ]
    assert detect_repeat_calls(turns) == []


def test_normalize_args_non_json_text():
    assert normalize_args("code=600519") == normalize_args("code=600519")
    assert normalize_args({"b": 2, "a": 1}) == '{"a":1,"b":2}'


# ── 跨轮数字矛盾 ─────────────────────────────────────────────────────

def test_number_conflict_same_metric_different_values():
    turns = [
        _turn(1, answer="600519 收盘价 1680.5 元"),
        _turn(2, answer="600519 收盘价 1750.0 元"),
    ]
    conflicts = detect_number_conflicts(turns)
    assert len(conflicts) == 1
    assert conflicts[0]["code"] == "600519"
    assert conflicts[0]["metric"] == "收盘价"
    values = {v["turn_index"]: v["value"] for v in conflicts[0]["values"]}
    assert values[1] == pytest_approx(1680.5) and values[2] == pytest_approx(1750.0)


def pytest_approx(x):
    import pytest
    return pytest.approx(x)


def test_number_conflict_negative_consistent_restatement():
    """跨轮重述同一数值（含千分位/万单位等价）→ 不报矛盾。"""
    turns = [
        _turn(1, answer="600519 收盘价 1680.5 元"),
        _turn(2, answer="600519 收盘价 1,680.5 元，量能平稳"),
    ]
    assert detect_number_conflicts(turns) == []


def test_number_conflict_negative_different_metrics():
    """不同指标（收盘价 vs 涨幅）数值不同是正常并列 → 不报矛盾。"""
    turns = [
        _turn(1, answer="600519 收盘价 1680.5 元"),
        _turn(2, answer="600519 涨幅 3.2%"),
    ]
    assert detect_number_conflicts(turns) == []


def test_number_conflict_percent_ratio_tolerance():
    """「涨跌幅 3.2%」↔「涨跌幅 0.032」比值↔百分数等价 → 不报矛盾。"""
    turns = [
        _turn(1, answer="600519 涨跌幅 3.2%"),
        _turn(2, answer="600519 涨跌幅 0.032"),
    ]
    assert detect_number_conflicts(turns) == []


def test_number_conflict_multi_code_attribution():
    """多代码按邻近归属：只有 600519 的数值变了 → 只报 600519。"""
    turns = [
        _turn(1, answer="600519 收盘价 1680.5 元，000001 收盘价 12.5 元"),
        _turn(2, answer="600519 收盘价 1750.0 元，000001 收盘价 12.5 元"),
    ]
    conflicts = detect_number_conflicts(turns)
    assert len(conflicts) == 1
    assert conflicts[0]["code"] == "600519"


# ── 否定约束 ─────────────────────────────────────────────────────────

def test_constraint_violation_in_later_turns():
    turns = [
        _turn(1, question="帮我分析 600519，不要看行业板块",
              answer="600519 基本面稳，行业板块景气度向上"),
        _turn(2, question="再对比一下 000001",
              answer="对比行业板块后，000001 弹性更大"),
    ]
    vs = detect_constraint_violations(turns)
    assert len(vs) == 2
    assert all(v["object"] == "行业板块" for v in vs)
    assert {(v["stated_turn"], v["violate_turn"], v["where"]) for v in vs} == {
        (1, 1, "answer"), (1, 2, "answer")}


def test_constraint_violation_in_tool_call():
    turns = [
        _turn(1, question="看下贵州茅台，无需查新闻",
              tool_calls=[_tc("mx_search_news", '{"keyword":"贵州茅台 新闻"}', 3)]),
    ]
    vs = detect_constraint_violations(turns)
    assert len(vs) == 1
    assert vs[0]["where"] == "tool" and vs[0]["violate_turn"] == 1


def test_constraint_violation_negative_no_constraints():
    """无否定式约束、无出现 → 空结果（负例）。"""
    turns = [
        _turn(1, question="帮我看看 600519", answer="综合基本面不错"),
        _turn(2, question="再看看 000001", answer="也可以"),
    ]
    assert detect_constraint_violations(turns) == []


# ── 汇总 ─────────────────────────────────────────────────────────────

def test_summarize_cross_turn_aggregates_three_rules():
    turns = [
        _turn(1, question="分析 600519，不要看行业板块",
              answer="600519 收盘价 1680.5 元",
              tool_calls=[_tc("ak_hist", '{"code":"600519"}', 1)]),
        _turn(2, question="再看看",
              answer="600519 收盘价 1700.0 元，行业板块走强",
              tool_calls=[_tc("ak_hist", '{"code":"600519"}', 2)]),
    ]
    summary = summarize_cross_turn(turns)
    assert len(summary["repeat_calls"]) == 1
    assert len(summary["number_conflicts"]) == 1
    assert len(summary["constraint_violations"]) == 1
    v = summary["constraint_violations"][0]
    assert (v["stated_turn"], v["violate_turn"]) == (1, 2)

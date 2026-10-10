# -*- coding: utf-8 -*-
"""eval.metrics.scoring.score_case 单元测试（不依赖 LLM / 网络）。

回归层 7 条规则维度（见 eval/dimensions.py）：
tool_selection / tool_execution / output_completeness / data_freshness /
context_efficiency / budget / constraint_compliance
§7.2 契约：每个产分维度的 detail 必须带 formula。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

import pytest

from eval.dimensions import PROFILE_REGRESSION, dimensions_for
from eval.metrics.scoring import ScoringAggregator, score_case


def _case(**overrides):
    case = {
        "id": "test_001",
        "category": "basic",
        "difficulty": "easy",
        "question": "贵州茅台最新收盘价是多少？",
        "expected_tools": ["stock_query"],
        "expected_sections": ["收盘价"],
        "enabled": 1,
    }
    case.update(overrides)
    return case


def _run_data(**overrides):
    data = {
        "benchmark_id": "test_001",
        "status": "success",
        "agent_response": "贵州茅台最新收盘价为 1500.00 元。",
        "tool_calls": [{"name": "stock_query", "input": '{"code": "600519"}',
                        "output": "1500.00", "success": True}],
        "trace_run_id": None,
        "iteration_count": 2,
        "tool_calls_count": 1,
        "tokens_in": 500,
        "tokens_out": 300,
        "elapsed_seconds": 3.0,
        "limit_reason": None,
        "error": None,
    }
    data.update(overrides)
    return data


# ── 维度齐全性与契约 ──────────────────────────────────────────────────
def test_registry_weights_cover_score_case_dims():
    """注册表回归层 7 条 = 聚合器缺省权重键。"""
    assert set(ScoringAggregator.DEFAULT_WEIGHTS) == \
        {d.key for d in dimensions_for(PROFILE_REGRESSION)}


def test_score_case_returns_dimensions_and_overall():
    out = score_case(_case(), _run_data())
    assert "dimensions" in out and "overall" in out
    # 无 trace 时新鲜度跳过，其余 6 条规则维度必须产出
    assert "data_freshness" not in out["dimensions"]
    assert set(out["dimensions"]) == {
        "tool_selection", "tool_execution", "output_completeness",
        "context_efficiency", "budget", "constraint_compliance",
    }
    for dim, d in out["dimensions"].items():
        assert {"score", "weight", "evaluator", "is_auto", "detail", "reason"} <= set(d), dim
        assert 0 <= d["score"] <= 100, dim


def test_every_dimension_detail_has_formula():
    """§7.2 契约：详情页必须能展示计算过程。"""
    out = score_case(_case(), _run_data())
    for dim, d in out["dimensions"].items():
        assert d["detail"].get("formula"), f"{dim} 缺 formula"


def test_overall_is_weighted_normalized():
    out = score_case(_case(), _run_data())
    weighted = sum(d["score"] * d["weight"] for d in out["dimensions"].values())
    total_w = sum(d["weight"] for d in out["dimensions"].values())
    assert out["overall"] == pytest.approx(weighted / total_w, abs=0.1)


# ── 工具选择 / 执行（拆分后的两条）────────────────────────────────────
def test_perfect_match_high_scores():
    out = score_case(_case(), _run_data())
    assert out["dimensions"]["tool_selection"]["score"] == pytest.approx(100, abs=1)
    assert out["dimensions"]["tool_execution"]["score"] == pytest.approx(100, abs=1)
    assert out["dimensions"]["output_completeness"]["score"] == pytest.approx(100, abs=1)


def test_missing_tools_lower_selection_not_execution():
    """漏选工具扣「选择」分；那次调用本身成功则「执行」分不受影响。"""
    out = score_case(
        _case(),
        _run_data(tool_calls=[{"name": "other_tool", "input": "{}",
                               "output": "x", "success": True}]),
    )
    sel = out["dimensions"]["tool_selection"]
    exe = out["dimensions"]["tool_execution"]
    assert sel["score"] == 0.0                     # F1 = 0
    assert sel["detail"]["missing"] == ["stock_query"]
    assert exe["score"] == pytest.approx(100, abs=1)   # 调用本身成功


def test_failed_tool_hits_execution_only():
    out = score_case(
        _case(),
        _run_data(tool_calls=[{"name": "stock_query", "input": "{}",
                               "output": "", "success": False, "error": "断连"}]),
    )
    exe = out["dimensions"]["tool_execution"]
    assert exe["score"] == 0.0
    assert exe["detail"]["failed_count"] == 1
    assert out["dimensions"]["tool_selection"]["score"] == pytest.approx(100, abs=1)


def test_db_json_string_fields():
    """DB 行的 expected_tools/expected_sections 是 JSON 字符串，必须正确解析。"""
    case = _case(expected_tools='["stock_query"]', expected_sections='["收盘价"]')
    out = score_case(case, _run_data())
    assert out["dimensions"]["tool_selection"]["score"] == pytest.approx(100, abs=1)


# ── 上下文效率 / 约束 ────────────────────────────────────────────────
def test_duplicate_same_args_calls_penalized():
    calls = [
        {"name": "get_spot", "input": '{"code": "600519"}', "success": True},
        {"name": "get_spot", "input": '{"code": "600519"}', "success": True},
        {"name": "get_spot", "input": '{"code": "600519"}', "success": True},
    ]
    out = score_case(_case(), _run_data(tool_calls=calls))
    ce = out["dimensions"]["context_efficiency"]
    assert ce["score"] == 50.0                      # 100 - 2×25
    assert ce["detail"]["duplicate_count"] == 2


def test_different_args_not_flagged():
    calls = [
        {"name": "get_spot", "input": '{"code": "600519"}', "success": True},
        {"name": "get_spot", "input": '{"code": "000001"}', "success": True},
    ]
    out = score_case(_case(), _run_data(tool_calls=calls))
    assert out["dimensions"]["context_efficiency"]["score"] == 100.0


def test_negative_constraint_violation_detected():
    case = _case(question="不要看行业板块，直接给个股结论")
    # 现实场景：板块名出现在工具参数里（工具名本身是英文，字符串匹配靠参数/回答）
    calls = [{"name": "stock_board_industry_hist_em",
              "input": '{"board": "行业板块"}', "success": True}]
    out = score_case(case, _run_data(tool_calls=calls))
    cc = out["dimensions"]["constraint_compliance"]
    assert cc["score"] == 50.0                     # 1 条违规 × 扣 50
    assert cc["detail"]["violations"], "调用板块工具应判违规"
    assert cc["detail"]["violations"][0]["object"] == "行业板块"


def test_negative_constraint_respected():
    case = _case(question="不要看行业板块，直接给个股结论")
    out = score_case(case, _run_data())     # 只调了个股工具
    cc = out["dimensions"]["constraint_compliance"]
    assert cc["score"] == 100.0
    assert len(cc["detail"]["constraints"]) == 1


def test_no_constraints_full_score():
    out = score_case(_case(), _run_data())
    assert out["dimensions"]["constraint_compliance"]["score"] == 100.0


# ── 预算 ─────────────────────────────────────────────────────────────
def test_budget_over_threshold_penalized():
    out = score_case(_case(), _run_data(iteration_count=20, elapsed_seconds=120))
    b = out["dimensions"]["budget"]
    assert b["score"] < 100
    assert b["detail"]["step_score"] < 100


# ── 自定义权重 ───────────────────────────────────────────────────────
def test_custom_weights_override():
    out = score_case(_case(), _run_data(),
                     weights={"tool_selection": 0.5})
    # 覆盖的维度用新权重，其余回退注册表权重
    assert out["dimensions"]["tool_selection"]["weight"] == 0.5
    assert out["dimensions"]["budget"]["weight"] == 0.10

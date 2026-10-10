"""详情契约单测（§11.4）：case 与 review 同构 + 路由接线。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

import pytest

from eval.metrics.detail import (
    build_case_detail_contract,
    build_review_detail_contract,
)


def _case_row(**over):
    row = {
        "id": 7, "eval_run_id": "run1", "benchmark_id": "bc_1",
        "question": "600519 现在什么位置？", "trace_run_id": "tr_9",
        "status": "scored", "overall_score": 82.3, "error": None,
        "scored_at": "2026-09-19T01:00:00",
    }
    row.update(over)
    return row


def _score_rows():
    return [
        {"dimension": "tool_selection", "score": 76.9, "weight": 0.2,
         "detail_json": '{"formula": "F1 0.77 × 100 = 76.9", "missing": ["x"]}',
         "reason": "漏选 x", "is_auto": 1, "provenance": "measured"},
        {"dimension": "risk_disclosure", "score": 30.0, "weight": 0.15,
         "detail_json": "{}", "reason": "单边结论", "is_auto": 0,
         "provenance": "measured"},
    ]


def test_case_contract_shape():
    out = build_case_detail_contract(_case_row(), _score_rows())
    assert out["target"]["case_id"] == 7
    assert out["target"]["trace_run_id"] == "tr_9"
    assert out["scores"]["rule"] == 82.3 and out["scores"]["diagnostic"] is None
    assert out["meta"]["rubric_version"]
    assert len(out["dimensions"]) == 2

    d0 = out["dimensions"][0]
    assert d0["label"] == "工具选择"          # label 来自注册表
    assert d0["judge"] == "rule"
    assert d0["basis_type"] == "rule"
    assert d0["basis"]["formula"] == "F1 0.77 × 100 = 76.9"
    assert d0["provenance"] == "measured"


def test_case_contract_unknown_dimension_falls_back():
    rows = [{"dimension": "legacy_dim", "score": 60, "weight": 0.1,
             "detail_json": None, "reason": "", "is_auto": 1,
             "provenance": "migrated"}]
    out = build_case_detail_contract(_case_row(), rows)
    d = out["dimensions"][0]
    assert d["label"] == "legacy_dim"
    assert d["provenance"] == "migrated"
    assert d["basis"] == {}


def test_review_contract_shape_and_findings_merge():
    review = {
        "review_uuid": "rv1", "scope": "run", "target_id": "tr_9",
        "rubric_version": "v2", "judge_model": "m", "status": "done",
        "overall": 61.5, "error": None, "duration_ms": 123.0,
        "scores": {"risk_disclosure": 30.0, "data_groundedness": 55.0},
        "findings": [{
            "dimension": "risk_disclosure", "verdict": "单边看多",
            "issues": ["无风险提示"], "suggestions": ["加 slot"],
            "evidence": [{"source": "answer", "step_ref": 0, "quote": "上行空间明确",
                          "verified": True}],
        }],
        "inputs": {"evidence": {"low_confidence": {"risk_disclosure": True}}},
    }
    out = build_review_detail_contract(review)
    assert out["scores"]["diagnostic"] == 61.5 and out["scores"]["rule"] is None
    by_dim = {d["dimension"]: d for d in out["dimensions"]}
    rd = by_dim["risk_disclosure"]
    assert rd["basis_type"] == "llm"
    assert rd["basis"]["verdict"] == "单边看多"
    assert rd["basis"]["evidence"][0]["quote"] == "上行空间明确"
    assert rd["low_confidence"] is True
    # findings 原样透出（§8 契约）
    assert out["findings"][0]["suggestions"] == ["加 slot"]


# ── 路由接线 ──────────────────────────────────────────────────────────
def test_routes_dimensions_and_case_detail(monkeypatch, tmp_path):
    import server.routes.eval as eval_route
    from eval.storage.eval_db import EvalStorage

    st = EvalStorage(db_path=tmp_path / "eval.db")
    try:
        monkeypatch.setattr(eval_route, "get_eval_storage", lambda: st)
        run_id = st.create_run(mode="react_stock", tag="t", total_cases=1)
        bm = {"id": "bc1", "category": "c", "difficulty": "easy",
              "question": "q", "expected_tools": ["t"]}
        cid = st.create_eval_case(run_id, bm)
        st.update_eval_case(case_id=cid, status="scored", overall_score=80.0)
        st.save_score(case_id=cid, dimension="tool_selection", score=80.0,
                      weight=0.2, evaluator="ToolSelectionEvaluator",
                      is_auto=True, detail={"formula": "F1 1.00 × 100 = 80.0"},
                      reason="ok")

        dims = eval_route.get_eval_dimensions()
        keys = {d["key"] for d in dims["dimensions"]}
        assert {"tool_selection", "risk_disclosure", "context_carryover"} <= keys
        assert dims["rubric_version"]

        # 旧结构保留 + 追加 contract（只增字段）
        out = eval_route.get_case_detail(cid)
        assert out["contract"]["target"]["case_id"] == cid
        assert out["contract"]["scores"]["rule"] == 80.0
        assert out["contract"]["dimensions"][0]["basis"]["formula"].startswith("F1")
        assert out["question"] == "q"          # 旧字段原样
        assert isinstance(out["scores"], list)  # 旧 scores 行结构保留

        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            eval_route.get_case_detail(99999)
    finally:
        st.close()

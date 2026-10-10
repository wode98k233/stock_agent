"""单次评测详情契约（§11.4）：三入口共用同一结构。

把 eval.db 的 case 行 + eval_scores 行折成统一契约，前端 `eval-detail.js`
只消费这一个结构；Agent 研判（reviews）走 `build_review_detail_contract`
产出同构数据。
"""
from __future__ import annotations

import json

from eval.dimensions import get_dimension
from eval.review.rubrics import RUBRIC_VERSION


def _loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def build_case_detail_contract(case: dict, scores: list[dict]) -> dict:
    """回归层 case 详情契约。

    Args:
        case: eval_cases 行（get_case_detail 返回，含 scores 键也可拆开传）
        scores: eval_scores 行列表
    """
    dims_out = []
    for s in scores or []:
        dim_key = s.get("dimension") or ""
        try:
            spec = get_dimension(dim_key)
            label, judge = spec.label, spec.judge
        except KeyError:
            label, judge = dim_key, "rule"      # 历史数据可能带旧维度名
        detail = _loads(s.get("detail_json")) or {}
        low_conf = bool((detail.get("low_confidence") or {})) if isinstance(detail, dict) else False
        dims_out.append({
            "dimension": dim_key,
            "label": label,
            "score": s.get("score"),
            "weight": s.get("weight"),
            "judge": judge,
            "basis_type": "rule",
            "basis": detail,                     # §7.2 计算明细（formula/threshold）
            "low_confidence": low_conf,
            "provenance": s.get("provenance") or "measured",
            "is_auto": bool(s.get("is_auto", 1)),
            "reason": s.get("reason") or "",
        })

    rule_overall = case.get("overall_score")

    return {
        "target": {
            "case_id": case.get("id"),
            "benchmark_id": case.get("benchmark_id"),
            "question": case.get("question") or "",
            "trace_run_id": case.get("trace_run_id"),
            "status": case.get("status") or "",
        },
        "scores": {
            "rule": rule_overall,
            "diagnostic": None,          # 回归 case 本身不产研判分；有联审时由前端合并
        },
        "meta": {
            "rubric_version": RUBRIC_VERSION,
            "scored_at": case.get("scored_at"),
            "error": case.get("error"),
        },
        "dimensions": dims_out,
        "findings": [],                  # 回归层无 LLM findings；联审 findings 由 reviews 接口提供
    }


def build_review_detail_contract(review: dict) -> dict:
    """Agent 研判（reviews 行）详情契约——与 case 详情同构。

    review 来自 `eval/review/store.get_review`（scores/findings/inputs 已解 JSON）。
    """
    scores = review.get("scores") or {}
    findings = review.get("findings") or []
    inputs = review.get("inputs") or {}

    dims_out = []
    for dim_key, score in scores.items():
        try:
            spec = get_dimension(dim_key)
            label, judge = spec.label, spec.judge
        except KeyError:
            label, judge = dim_key, "llm"
        low_conf = bool((inputs.get("evidence") or {}).get("low_confidence", {}).get(dim_key))
        dims_out.append({
            "dimension": dim_key,
            "label": label,
            "score": score,
            "weight": None,              # 研判维度权重由注册表声明，前端可从 /dimensions 取
            "judge": judge,
            "basis_type": "llm",
            "basis": {"verdict": "", "reason": "", "evidence": []},
            "low_confidence": low_conf,
            "provenance": "measured",
            "is_auto": False,
            "reason": "",
        })

    # findings 里的 verdict/reason/evidence 挂到对应维度，丰富 basis
    by_dim: dict[str, dict] = {d["dimension"]: d for d in dims_out}
    for f in findings:
        d = by_dim.get(str(f.get("dimension") or ""))
        if not d:
            continue
        d["basis"] = {
            "verdict": f.get("verdict") or "",
            "reason": "; ".join(f.get("issues") or []),
            "evidence": f.get("evidence") or [],
        }
        d["basis_type"] = "llm" if not d["basis"]["evidence"] else "llm"

    return {
        "target": {
            "review_uuid": review.get("review_uuid"),
            "scope": review.get("scope"),
            "target_id": review.get("target_id"),
            "question": "",
        },
        "scores": {
            "rule": None,
            "diagnostic": review.get("overall"),
        },
        "meta": {
            "rubric_version": review.get("rubric_version"),
            "judge_model": review.get("judge_model"),
            "status": review.get("status"),
            "error": review.get("error"),
            "duration_ms": review.get("duration_ms"),
            "evidence": inputs.get("evidence") or {},
        },
        "dimensions": dims_out,
        "findings": findings,
    }

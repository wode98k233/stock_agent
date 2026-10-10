"""回归层打分聚合。

维度与权重**从 `eval/dimensions.py` 注册表派生**（profiles=regression 的 7 条，
全规则、可复现）。每个规则维度的 detail 必须带 `formula` 或 `threshold`
（§7.2 契约：详情页要能回答「为什么评这个分」）。

设计文档：docs/superpowers/specs/2026-09-18-eval-redesign-design.md §3/§6.1/§7.2
"""
import json
from typing import Dict, List

from eval.dimensions import PROFILE_REGRESSION, weights_for
from eval.evaluators.base_evaluator import EvalResult
from eval.evaluators.budget_evaluator import BudgetEvaluator
from eval.evaluators.context_efficiency_evaluator import ContextEfficiencyEvaluator
from eval.evaluators.constraint_evaluator import ConstraintComplianceEvaluator
from eval.evaluators.data_freshness_evaluator import DataFreshnessEvaluator
from eval.evaluators.output_evaluator import OutputEvaluator
from eval.evaluators.tool_evaluator import ToolExecutionEvaluator, ToolSelectionEvaluator


class ScoringAggregator:
    """回归层加权聚合（权重缺省取注册表；同一 profile 内聚合时归一化）。"""

    DEFAULT_WEIGHTS: Dict[str, float] = weights_for(PROFILE_REGRESSION)

    def __init__(self, weights: Dict[str, float] | None = None):
        self.weights = weights or self.DEFAULT_WEIGHTS

    def aggregate(self, results: List[EvalResult]) -> Dict:
        """聚合多个评测结果，返回总分和明细。score<0 表示跳过该维度。"""
        total_weight = 0.0
        weighted_sum = 0.0
        dimensions = {}
        reasons = []

        for r in results:
            if r.score < 0:  # 跳过未评测的维度
                continue
            w = self.weights.get(r.dimension, r.weight)
            weighted_sum += r.score * w
            total_weight += w
            dimensions[r.dimension] = {
                "score": round(r.score, 1),
                "weight": w,
                "is_auto": r.is_auto,
                "reason": r.reason,
                "detail": r.detail,
            }
            if r.reason:
                reasons.append(f"{r.dimension}: {r.reason}")

        overall = weighted_sum / total_weight if total_weight > 0 else 0

        return {
            "overall": round(overall, 1),
            "dimensions": dimensions,
            "summary": "; ".join(reasons) if reasons else "评测完成",
        }


def _parse_maybe_json(value):
    """benchmark 字段兼容：DB 行是 JSON 字符串，json 数据集是原生 list。"""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def score_case(
    benchmark_case: Dict | None,
    run_data: Dict,
    weights: Dict[str, float] | None = None,
) -> Dict:
    """对单个 case 的运行结果执行回归层评测（Web / CLI 共用，全规则）。

    Args:
        benchmark_case: benchmark 行（expected_tools/expected_sections/
            difficulty 等，兼容 JSON 字符串或原生 list）。**传 None 表示
            存量回测的无题库模式**（§4.2）：`tool_selection` /
            `output_completeness` 因期望证据缺失自动跳过（权重重归一化），
            约束维度回退用 `run_data["question"]`（run 原始提问）。
        run_data: AgentRunner.run_single() 的返回（agent_response/tool_calls/
            iteration_count/tokens_in/tokens_out/elapsed_seconds/trace_run_id）。
        weights: 覆盖注册表默认权重。

    Returns:
        {"dimensions": {dim: {score, weight, evaluator, is_auto, detail, reason}},
         "overall": float, "summary": str}
    """
    has_benchmark = benchmark_case is not None
    case = dict(benchmark_case) if has_benchmark else {
        "question": run_data.get("question") or "",
    }
    case["expected_tools"] = _parse_maybe_json(case.get("expected_tools"))
    case["expected_sections"] = _parse_maybe_json(case.get("expected_sections"))

    agent_response = run_data.get("agent_response", "") or ""
    tool_calls = run_data.get("tool_calls", []) or []
    run_metrics = {
        "iteration_count": run_data.get("iteration_count", 0) or 0,
        "tokens_in": run_data.get("tokens_in", 0) or 0,
        "tokens_out": run_data.get("tokens_out", 0) or 0,
        "elapsed_seconds": run_data.get("elapsed_seconds", 0) or 0,
    }
    trace_run_id = run_data.get("trace_run_id")

    evaluators = [
        ToolExecutionEvaluator(),
        DataFreshnessEvaluator(),
        ContextEfficiencyEvaluator(),
        BudgetEvaluator(),
        ConstraintComplianceEvaluator(),
    ]
    if has_benchmark:
        # 期望类维度只在有题库时评测（无题库自动跳过 → 权重重归一化，§4.2）
        evaluators = [ToolSelectionEvaluator(), OutputEvaluator()] + evaluators

    results: List[EvalResult] = []
    evaluator_names: Dict[str, str] = {}
    for evaluator in evaluators:
        try:
            r = evaluator.evaluate(
                benchmark_case=case,
                agent_response=agent_response,
                tool_calls=tool_calls,
                run_metrics=run_metrics,
                trace_run_id=trace_run_id,
            )
            results.append(r)
            evaluator_names[r.dimension] = type(evaluator).__name__
        except Exception:
            # 单个评测器失败跳过该维度，不阻断整体打分
            continue

    aggregator = ScoringAggregator(weights)
    agg = aggregator.aggregate(results)

    dimensions = {}
    for r in results:
        if r.score < 0:   # 跳过的维度不进 dimensions（与 aggregate 口径一致）
            continue
        dimensions[r.dimension] = {
            "score": round(r.score, 1),
            "weight": aggregator.weights.get(r.dimension, r.weight),
            "evaluator": evaluator_names.get(r.dimension, "auto"),
            "is_auto": r.is_auto,
            "detail": r.detail,
            "reason": r.reason,
        }

    return {
        "dimensions": dimensions,
        "overall": agg["overall"],
        "summary": agg["summary"],
    }

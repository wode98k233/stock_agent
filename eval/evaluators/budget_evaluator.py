"""预算消耗评测器（自动，规则判定）。

对应维度注册表 `budget`（旧 `efficiency` 改名而来，口径不变）。
按难度阈值对步数 / token / 耗时超标扣分。
"""
from typing import Any, Dict, List

from .base_evaluator import BaseEvaluator, EvalResult


class BudgetEvaluator(BaseEvaluator):
    """预算消耗：步数 40% + token 30% + 时间 30%，按难度阈值。"""

    dimension = "budget"
    weight = 0.10
    is_auto = True

    DEFAULT_THRESHOLDS = {
        "easy":   {"steps": 3,  "tokens": 2000,  "time": 8},
        "medium": {"steps": 6,  "tokens": 4000,  "time": 15},
        "hard":   {"steps": 10, "tokens": 8000,  "time": 30},
    }

    def __init__(self, thresholds: Dict | None = None):
        self.thresholds = thresholds or self.DEFAULT_THRESHOLDS

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        difficulty = benchmark_case.get("difficulty", "medium")
        thresh = self.thresholds.get(difficulty, self.thresholds["medium"])

        iteration_count = run_metrics.get("iteration_count", 0)
        total_tokens = run_metrics.get("tokens_in", 0) + run_metrics.get("tokens_out", 0)
        elapsed = run_metrics.get("elapsed_seconds", 0)

        steps_excess = max(0, iteration_count - thresh["steps"])
        step_score = max(0, 100 - steps_excess * 15)
        tokens_excess_ratio = max(0, (total_tokens - thresh["tokens"]) / thresh["tokens"])
        token_score = max(0, 100 - tokens_excess_ratio * 100)
        time_excess_ratio = max(0, (elapsed - thresh["time"]) / thresh["time"])
        time_score = max(0, 100 - time_excess_ratio * 100)

        score = step_score * 0.4 + token_score * 0.3 + time_score * 0.3

        detail: Dict = {
            "difficulty": difficulty,
            "thresholds": thresh,
            "actual_steps": iteration_count,
            "actual_tokens": total_tokens,
            "actual_time": elapsed,
            "step_score": round(step_score, 1),
            "token_score": round(token_score, 1),
            "time_score": round(time_score, 1),
            "formula": (f"步数 {step_score:.2f}×0.4 + token {token_score:.2f}×0.3"
                        f" + 时间 {time_score:.2f}×0.3 = {score:.1f}"),
        }

        reasons = []
        if steps_excess > 0:
            reasons.append(f"步数超标 {steps_excess} 步")
        if tokens_excess_ratio > 0:
            reasons.append(f"Token 超标 {tokens_excess_ratio:.0%}")
        if time_excess_ratio > 0:
            reasons.append(f"耗时超标 {time_excess_ratio:.0%}")
        if not reasons:
            reasons.append("预算内完成")

        return EvalResult(
            dimension=self.dimension, score=round(score, 1), weight=self.weight,
            is_auto=self.is_auto, detail=detail, reason="; ".join(reasons),
        )

"""数据新鲜度评测器（自动，规则判定）。

对应维度注册表 `data_freshness`：工具返回的数据日期相对 **run 执行时刻** 是否过期。
复用 `eval/evidence/grounding.py` 的阈值分档与判定（单一实现，不另写口径）。
无 trace_run_id / trace 不可用时跳过该维度（score=-1，聚合时重归一化）。
"""
from typing import Any, Dict, List

from .base_evaluator import BaseEvaluator, EvalResult


def _freshness_score(lag_days: int, threshold: int) -> tuple[float, str]:
    """按超出倍数分档扣分（与设计文档 §11.3 示例一致）。"""
    ratio = lag_days / threshold if threshold > 0 else float("inf")
    if ratio <= 1:
        return 100.0, f"滞后 {lag_days} 天 ≤ 阈值 {threshold} 天"
    if ratio <= 3:
        return 80.0, f"滞后 {lag_days} 天（阈值的 {ratio:.1f} 倍）→ 80"
    if ratio <= 10:
        return 60.0, f"滞后 {lag_days} 天（阈值的 {ratio:.1f} 倍）→ 60"
    return 40.0, f"滞后 {lag_days} 天（阈值的 {ratio:.1f} 倍）→ 40"


class DataFreshnessEvaluator(BaseEvaluator):
    """数据新鲜度：工具返回里的最大日期距 run 执行时刻是否超阈值。"""

    dimension = "data_freshness"
    weight = 0.15
    is_auto = True

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        if not trace_run_id:
            return self._skip("无 trace_run_id，无法取工具返回日期")

        try:
            from utils.agent_trace.store import store
            from eval.evidence.grounding import check_freshness
            from eval.evidence.trace_source import load_run_evidence

            # 分库路由：按 run_id probe 月库；无任何月库（如单测环境）直接跳过
            if not store.iter_existing_months():
                return self._skip("trace 库不存在")
            evidence = load_run_evidence(store.conn_for_run(trace_run_id), trace_run_id)
        except Exception as e:      # trace 取证失败不阻断整体打分
            return self._skip(f"trace 取证失败: {e}")

        report = check_freshness(evidence)
        detail: Dict = {
            **report.to_dict(),
            "threshold_basis": "项目 stale_threshold_days 分档（日频 10 / 月频 45 / 季频 135）",
        }

        if report.lag_days is None:
            detail["formula"] = "工具返回中无可判定日期 → 跳过"
            return EvalResult(
                dimension=self.dimension, score=-1.0, weight=self.weight,
                is_auto=self.is_auto, detail=detail,
                reason="工具返回中无可判定日期，跳过",
            )

        score, explain = _freshness_score(report.lag_days, report.threshold_days)
        detail["formula"] = explain
        return EvalResult(
            dimension=self.dimension, score=score, weight=self.weight,
            is_auto=self.is_auto, detail=detail,
            reason=explain,
        )

    @staticmethod
    def _skip(reason: str) -> EvalResult:
        return EvalResult(
            dimension="data_freshness", score=-1.0, weight=0.15, is_auto=True,
            detail={"formula": f"跳过: {reason}"}, reason=f"跳过: {reason}",
        )

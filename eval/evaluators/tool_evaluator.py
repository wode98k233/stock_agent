"""工具选择 / 工具执行评测器（自动，规则判定）。

对应维度注册表（eval/dimensions.py）回归层的两条：
- tool_selection：选没选对（F1），从旧 tool_correctness 拆出
- tool_execution：执行得好不好（成功率），从旧 tool_correctness 拆出

§7.2 契约：detail 必须带 `formula`（详情页展示计算过程）。
"""
from typing import Any, Dict, List

from .base_evaluator import BaseEvaluator, EvalResult


class ToolSelectionEvaluator(BaseEvaluator):
    """工具选择：期望工具集 vs 实际调用集合的 F1。"""

    dimension = "tool_selection"
    weight = 0.20
    is_auto = True

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        expected = set(benchmark_case.get("expected_tools") or [])
        actual = {tc.get("name", "") for tc in tool_calls if tc.get("name")}

        detail: Dict = {
            "expected": sorted(expected),
            "actual": sorted(actual),
            "missing": sorted(expected - actual),
            "extra": sorted(actual - expected),
        }

        if not expected:
            # 无期望工具：调了工具属于多选（无法判定必要性），给部分分
            score = 100.0 if not tool_calls else 70.0
            detail["formula"] = "无期望工具" + ("" if not tool_calls else "；调用了工具按多选扣至 70")
            return EvalResult(
                dimension=self.dimension, score=score, weight=self.weight,
                is_auto=self.is_auto, detail=detail,
                reason="无期望工具，未调用工具（合理）" if not tool_calls
                else f"无期望工具但调用了 {len(tool_calls)} 个工具",
            )

        tp = len(expected & actual)
        precision = tp / len(actual) if actual else 0.0
        recall = tp / len(expected) if expected else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        score = f1 * 100

        detail.update({
            "precision": round(precision, 2),
            "recall": round(recall, 2),
            "f1": round(f1, 2),
            "formula": f"F1 {f1:.2f} × 100 = {score:.1f}",
        })

        reasons = []
        if detail["missing"]:
            reasons.append(f"漏选: {', '.join(detail['missing'])}")
        if detail["extra"]:
            reasons.append(f"多选: {', '.join(detail['extra'])}")
        if not reasons:
            reasons.append("工具选择完全匹配")

        return EvalResult(
            dimension=self.dimension, score=round(score, 1), weight=self.weight,
            is_auto=self.is_auto, detail=detail, reason="; ".join(reasons),
        )


class ToolExecutionEvaluator(BaseEvaluator):
    """工具执行：调用成功率与失败明细（与「选得对不对」解耦）。"""

    dimension = "tool_execution"
    weight = 0.20
    is_auto = True

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        calls = [
            {"name": tc.get("name", ""), "ok": bool(tc.get("success", True)),
             "error": str(tc.get("error") or tc.get("extra", {}).get("error") or "")[:200]}
            for tc in tool_calls if tc.get("name")
        ]
        failed = [c for c in calls if not c["ok"]]
        success_rate = (len(calls) - len(failed)) / len(calls) if calls else 1.0
        score = success_rate * 100

        detail: Dict = {
            "calls": calls,
            "success_rate": round(success_rate, 2),
            "failed_count": len(failed),
            "formula": f"成功率 {success_rate:.2f} × 100 = {score:.1f}",
        }
        if not calls:
            detail["formula"] = "无工具调用，不适用 → 满分"

        reason = "全部调用成功" if not failed else \
            f"{len(failed)}/{len(calls)} 次调用失败: " + \
            ", ".join(dict.fromkeys(c["name"] for c in failed))

        return EvalResult(
            dimension=self.dimension, score=round(score, 1), weight=self.weight,
            is_auto=self.is_auto, detail=detail, reason=reason,
        )

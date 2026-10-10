"""
输出完整性评测器（自动）
"""
import re
from typing import Any, Dict, List

from .base_evaluator import BaseEvaluator, EvalResult


class OutputEvaluator(BaseEvaluator):
    """
    输出完整性评测
    - 检查输出是否包含期望的章节关键词
    """
    dimension = "output_completeness"
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
        expected_sections = benchmark_case.get("expected_sections") or []
        detail = {
            "expected_sections": expected_sections,
            "hit": [],
            "miss": [],
        }

        if not expected_sections:
            detail["formula"] = "无期望章节 → 满分"
            return EvalResult(
                dimension=self.dimension,
                score=100.0,
                weight=self.weight,
                is_auto=self.is_auto,
                detail=detail,
                reason="无期望章节，跳过评测",
            )

        response_lower = agent_response.lower()

        hit = []
        miss = []
        for section in expected_sections:
            # 支持模糊匹配：去掉空格后检查是否出现在响应中
            pattern = re.escape(section.lower())
            if re.search(pattern, response_lower):
                hit.append(section)
            else:
                miss.append(section)

        detail["hit"] = hit
        detail["miss"] = miss

        score = (len(hit) / len(expected_sections)) * 100 if expected_sections else 100
        detail["formula"] = f"命中 {len(hit)}/{len(expected_sections)} = {score:.1f}"

        reason = f"命中 {len(hit)}/{len(expected_sections)} 个章节"
        if miss:
            reason += f"，缺失: {', '.join(miss)}"

        return EvalResult(
            dimension=self.dimension,
            score=round(score, 1),
            weight=self.weight,
            is_auto=self.is_auto,
            detail=detail,
            reason=reason,
        )

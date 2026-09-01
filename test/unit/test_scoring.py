"""
评测打分聚合器单元测试
测试 eval/metrics/scoring.py 的 ScoringAggregator
"""
import pytest
from eval.evaluators.base_evaluator import EvalResult
from eval.metrics.scoring import ScoringAggregator


class TestScoringAggregator:
    """ScoringAggregator 单元测试"""

    def setup_method(self):
        self.weights = {
            "tool_correctness": 0.20,
            "output_completeness": 0.15,
            "factual_accuracy": 0.30,
            "reasoning_quality": 0.25,
            "efficiency": 0.10,
        }
        self.agg = ScoringAggregator(self.weights)

    def _make_result(self, dimension, score, is_auto=True, reason="", detail=None):
        return EvalResult(
            dimension=dimension,
            score=score,
            weight=self.weights.get(dimension, 0.1),
            is_auto=is_auto,
            detail=detail or {},
            reason=reason,
        )

    def test_all_dimensions_present(self):
        """所有 5 个维度都有分数时，总分 = 加权平均"""
        results = [
            self._make_result("tool_correctness", 80.0),
            self._make_result("output_completeness", 90.0),
            self._make_result("factual_accuracy", 70.0),
            self._make_result("reasoning_quality", 85.0),
            self._make_result("efficiency", 95.0),
        ]
        r = self.agg.aggregate(results)
        expected = (80*0.2 + 90*0.15 + 70*0.3 + 85*0.25 + 95*0.1)
        assert r["overall"] == pytest.approx(round(expected, 1), abs=0.1)
        assert len(r["dimensions"]) == 5

    def test_skip_negative_scores(self):
        """score < 0 的维度应被跳过（如 LLM-Judge 未配置时返回 -1）"""
        results = [
            self._make_result("tool_correctness", 80.0),
            self._make_result("output_completeness", 90.0),
            self._make_result("factual_accuracy", -1.0),  # 跳过
            self._make_result("reasoning_quality", -1.0),  # 跳过
            self._make_result("efficiency", 95.0),
        ]
        r = self.agg.aggregate(results)
        # 只用 tool(0.2) + output(0.15) + efficiency(0.1) 的权重
        expected = (80*0.2 + 90*0.15 + 95*0.1) / (0.2 + 0.15 + 0.1)
        assert r["overall"] == round(expected, 1)
        assert "factual_accuracy" not in r["dimensions"]

    def test_empty_results(self):
        """空结果列表应返回 overall=0"""
        r = self.agg.aggregate([])
        assert r["overall"] == 0
        assert r["dimensions"] == {}
        assert "评测完成" in r["summary"]

    def test_all_skipped(self):
        """所有维度都跳过时，总分=0"""
        results = [
            self._make_result("tool_correctness", -1.0),
            self._make_result("output_completeness", -1.0),
        ]
        r = self.agg.aggregate(results)
        assert r["overall"] == 0

    def test_single_dimension(self):
        """只有一个维度时，总分 = 该维度分数"""
        results = [self._make_result("efficiency", 75.0)]
        r = self.agg.aggregate(results)
        assert r["overall"] == 75.0

    def test_reason_aggregation(self):
        """多个维度的 reason 应拼接进 summary"""
        results = [
            self._make_result("tool_correctness", 80.0, reason="工具匹配良好"),
            self._make_result("efficiency", 95.0, reason="效率优秀"),
        ]
        r = self.agg.aggregate(results)
        assert "tool_correctness" in r["summary"]
        assert "工具匹配良好" in r["summary"]
        assert "效率优秀" in r["summary"]

    def test_default_weights(self):
        """不传 weights 时使用 DEFAULT_WEIGHTS"""
        agg = ScoringAggregator()
        assert agg.weights == ScoringAggregator.DEFAULT_WEIGHTS
        # 权重总和应 = 1.0
        total = sum(agg.weights.values())
        assert abs(total - 1.0) < 0.01

    def test_dimension_detail_preserved(self):
        """维度明细应保留在 dimensions 里"""
        detail = {"matched": 3, "missing": 1, "extra": 0}
        results = [
            self._make_result("tool_correctness", 75.0, detail=detail, reason="3/4 匹配"),
        ]
        r = self.agg.aggregate(results)
        assert r["dimensions"]["tool_correctness"]["detail"] == detail
        assert r["dimensions"]["tool_correctness"]["reason"] == "3/4 匹配"

    def test_score_rounding(self):
        """分数应四舍五入到 1 位小数"""
        results = [
            self._make_result("tool_correctness", 80.555),
            self._make_result("output_completeness", 90.444),
        ]
        r = self.agg.aggregate(results)
        for dim_info in r["dimensions"].values():
            # 每个 dimension score 四舍五入
            assert dim_info["score"] == round(dim_info["score"], 1)

    def test_unknown_dimension(self):
        """未知维度（不在 weights 里）应使用 EvalResult 自带的 weight"""
        result = EvalResult(
            dimension="custom_dim",
            score=50.0,
            weight=0.5,
            is_auto=False,
            detail={},
            reason="自定义维度",
        )
        r = self.agg.aggregate([result])
        assert "custom_dim" in r["dimensions"]
        assert r["dimensions"]["custom_dim"]["weight"] == 0.5

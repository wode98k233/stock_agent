"""
最小冒烟测试：不依赖 LLM，测评测器逻辑（回归层新维度）
"""
import sys
sys.path.insert(0, ".")

from eval.storage.eval_db import get_eval_storage
from eval.evaluators.tool_evaluator import ToolExecutionEvaluator, ToolSelectionEvaluator
from eval.evaluators.output_evaluator import OutputEvaluator
from eval.evaluators.budget_evaluator import BudgetEvaluator
from eval.evaluators.context_efficiency_evaluator import ContextEfficiencyEvaluator
from eval.evaluators.constraint_evaluator import ConstraintComplianceEvaluator
from eval.metrics.scoring import ScoringAggregator


def test_tool_selection_evaluator():
    print("=== ToolSelectionEvaluator ===")
    evaluator = ToolSelectionEvaluator()

    # Case 1: 完全正确
    result = evaluator.evaluate(
        benchmark_case={"expected_tools": ["stock_analysis"]},
        agent_response="",
        tool_calls=[
            {"name": "stock_analysis", "success": True},
            {"name": "stock_analysis", "success": True},
        ],
        run_metrics={},
    )
    print(f"  完全正确: {result.score:.1f} (期望 ~100)")
    assert result.score > 90, f"期望高分，实际 {result.score}"

    # Case 2: 漏掉一个工具（3 期望，命中 2）
    result = evaluator.evaluate(
        benchmark_case={"expected_tools": ["stock_analysis", "valuation"]},
        agent_response="",
        tool_calls=[
            {"name": "stock_analysis", "success": True},
            {"name": "stock_analysis", "success": True},
        ],
        run_metrics={},
    )
    print(f"  漏掉一个: {result.score:.1f} (F1=0.8, 期望 ~80)")
    assert 70 < result.score < 90, f"期望 ~80，实际 {result.score}"

    print("  [OK] ToolSelectionEvaluator")


def test_tool_execution_evaluator():
    print("=== ToolExecutionEvaluator ===")
    evaluator = ToolExecutionEvaluator()

    result = evaluator.evaluate(
        benchmark_case={},
        agent_response="",
        tool_calls=[
            {"name": "a", "success": True},
            {"name": "b", "success": False, "error": "断连"},
        ],
        run_metrics={},
    )
    print(f"  一半失败: {result.score:.1f} (期望 50)")
    assert result.score == 50, f"期望 50，实际 {result.score}"
    print("  [OK] ToolExecutionEvaluator")


def test_output_evaluator():
    print("=== OutputEvaluator ===")
    evaluator = OutputEvaluator()

    result = evaluator.evaluate(
        benchmark_case={"expected_sections": ["MACD", "金叉", "建议"]},
        agent_response="MACD 指标显示金叉，建议买入",
        tool_calls=[],
        run_metrics={},
    )
    print(f"  完整输出: {result.score:.1f} (期望 100)")
    assert result.score == 100, f"期望满分，实际 {result.score}"

    result = evaluator.evaluate(
        benchmark_case={"expected_sections": ["MACD", "金叉", "建议"]},
        agent_response="MACD 指标显示金叉",
        tool_calls=[],
        run_metrics={},
    )
    print(f"  缺失建议: {result.score:.1f} (期望 ~66)")
    assert 50 < result.score < 80, f"期望 ~66，实际 {result.score}"

    print("  [OK] OutputEvaluator")


def test_budget_evaluator():
    print("=== BudgetEvaluator ===")
    evaluator = BudgetEvaluator()

    result = evaluator.evaluate(
        benchmark_case={"difficulty": "easy"},
        agent_response="",
        tool_calls=[],
        run_metrics={"iteration_count": 2, "tokens_in": 1000, "tokens_out": 500, "elapsed_seconds": 5},
    )
    print(f"  预算内: {result.score:.1f} (期望高分)")
    assert result.score > 70, f"期望高分，实际 {result.score}"

    result = evaluator.evaluate(
        benchmark_case={"difficulty": "easy"},
        agent_response="",
        tool_calls=[],
        run_metrics={"iteration_count": 20, "tokens_in": 10000, "tokens_out": 5000, "elapsed_seconds": 60},
    )
    print(f"  严重超标: {result.score:.1f} (期望低分)")
    assert result.score < 50, f"期望低分，实际 {result.score}"

    print("  [OK] BudgetEvaluator")


def test_context_efficiency_evaluator():
    print("=== ContextEfficiencyEvaluator ===")
    evaluator = ContextEfficiencyEvaluator()

    result = evaluator.evaluate(
        benchmark_case={},
        agent_response="",
        tool_calls=[
            {"name": "get_spot", "input": '{"code": "600519"}', "success": True},
            {"name": "get_spot", "input": '{"code": "600519"}', "success": True},
        ],
        run_metrics={},
    )
    print(f"  重复同参调用: {result.score:.1f} (期望 75)")
    assert result.score == 75, f"期望 75，实际 {result.score}"
    print("  [OK] ContextEfficiencyEvaluator")


def test_constraint_evaluator():
    print("=== ConstraintComplianceEvaluator ===")
    evaluator = ConstraintComplianceEvaluator()

    result = evaluator.evaluate(
        benchmark_case={"question": "不要看行业板块，直接给个股结论"},
        agent_response="结论如下",
        tool_calls=[{"name": "stock_board_industry_hist_em",
                     "input": '{"board": "行业板块"}', "success": True}],
        run_metrics={},
    )
    print(f"  违反约束: {result.score:.1f} (期望 50 = 100 - 1×50)")
    assert result.score == 50, f"期望 50，实际 {result.score}"

    result = evaluator.evaluate(
        benchmark_case={"question": "直接给个股结论"},
        agent_response="结论如下",
        tool_calls=[],
        run_metrics={},
    )
    print(f"  无约束: {result.score:.1f} (期望 100)")
    assert result.score == 100, f"期望 100，实际 {result.score}"
    print("  [OK] ConstraintComplianceEvaluator")


def test_scoring_aggregator():
    print("=== ScoringAggregator ===")
    from eval.evaluators.base_evaluator import EvalResult

    aggregator = ScoringAggregator()

    results = [
        EvalResult(dimension="tool_selection", score=80, weight=0.2, is_auto=True, detail={}),
        EvalResult(dimension="output_completeness", score=90, weight=0.15, is_auto=True, detail={}),
        EvalResult(dimension="budget", score=70, weight=0.1, is_auto=True, detail={}),
    ]

    agg = aggregator.aggregate(results)
    print(f"  聚合总分: {agg['overall']:.1f}")
    assert "tool_selection" in agg["dimensions"]
    assert "output_completeness" in agg["dimensions"]
    print("  [OK] ScoringAggregator")


def test_db_crud():
    print("=== DB CRUD ===")
    storage = get_eval_storage()

    case = {
        "id": "test_001",
        "category": "stock_analysis",
        "difficulty": "easy",
        "question": "测试问题",
        "expected_tools": ["stock_analysis"],
        "expected_sections": ["MACD"],
        "reference_facts": {},
        "judge_criteria": [],
        "tags": ["test"],
        "enabled": 1,
    }
    storage.upsert_benchmark_case(case)
    print("  [OK] benchmark case 写入")

    cases = storage.list_benchmark()
    assert any(c["id"] == "test_001" for c in cases)
    print("  [OK] benchmark case 读取")

    import uuid
    run_id = f"test_{uuid.uuid4().hex[:8]}"
    storage.create_run(run_id=run_id, name="测试批次", mode="react_stock", tag="v0.1", config={}, llm_judge_model="gpt-4o")
    print("  [OK] eval_run 创建")

    run = storage.get_run(run_id)
    assert run is not None
    print("  [OK] eval_run 读取")

    storage.close()
    print("  [OK] DB CRUD")


if __name__ == "__main__":
    test_tool_selection_evaluator()
    test_tool_execution_evaluator()
    test_output_evaluator()
    test_budget_evaluator()
    test_context_efficiency_evaluator()
    test_constraint_evaluator()
    test_scoring_aggregator()
    test_db_crud()
    print("\n[OK] All tests passed!")

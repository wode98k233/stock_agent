"""
AgentRunContext 指标快照测试。
"""
import logging


def test_build_snapshot_uses_budget_status_fields_and_remaining_values():
    """预算快照应读取 BudgetController.get_status 的真实字段并计算剩余额。"""
    from agents.run_context import AgentRunContext
    from utils.budget import BudgetController, BudgetLimits
    from utils.logger import RequestContext, RadarLogger

    raw_logger = logging.getLogger("test.run_context.snapshot")
    radar_logger = RadarLogger(raw_logger, "snapshot")
    ctx = RequestContext("snapshot", raw_logger)
    ctx.record_llm(20, 10)

    budget = BudgetController(BudgetLimits(
        max_tokens_per_query=100,
        max_llm_calls_per_query=5,
        max_time_seconds=60,
    ))
    budget.add_tokens(30)
    budget.add_call()

    run_ctx = AgentRunContext("react_stock", radar_logger, "分析", budget=budget)
    snapshot = run_ctx._build_snapshot(ctx)

    assert snapshot["budget"]["tokens"] == 30
    assert snapshot["budget"]["tokens_remaining"] == 70
    assert snapshot["budget"]["calls"] == 1
    assert snapshot["budget"]["calls_remaining"] == 4
    assert snapshot["consistency"]["metrics_vs_budget_tokens"] == "ok"

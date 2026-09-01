"""backtest.db CRUD 集成测试

测试回测数据库的完整读写流程：
- 策略 CRUD
- 回测任务 CRUD
- 回测结果 CRUD
- 交易记录 CRUD
"""
import json
import pytest
from datetime import datetime

from utils.cache.backtest_db import (
    get_backtest_db,
    init_backtest_tables,
    # strategies
    get_strategies,
    get_strategy,
    upsert_strategy,
    # runs
    create_backtest_run,
    get_backtest_run,
    get_backtest_runs,
    update_backtest_run,
    # results
    save_backtest_result,
    get_backtest_result,
    # trades
    save_backtest_trades,
    get_backtest_trades,
    # stats
    get_data_stats,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


@pytest.fixture(autouse=True)
def _init_and_cleanup():
    """确保表结构已初始化，测试后清理测试数据"""
    init_backtest_tables()
    yield
    # 清理测试用数据
    with get_backtest_db() as conn:
        conn.execute("DELETE FROM backtest_trades WHERE run_id LIKE 'BT-TEST-%' OR run_id LIKE 'BT-LIMIT-%' OR run_id LIKE 'BT-RESULT-%' OR run_id LIKE 'BT-TRADE-%' OR run_id LIKE 'BT-CUSTOM-%'")
        conn.execute("DELETE FROM backtest_results WHERE run_id LIKE 'BT-TEST-%' OR run_id LIKE 'BT-LIMIT-%' OR run_id LIKE 'BT-RESULT-%' OR run_id LIKE 'BT-TRADE-%' OR run_id LIKE 'BT-CUSTOM-%'")
        conn.execute("DELETE FROM backtest_runs WHERE run_id LIKE 'BT-TEST-%' OR run_id LIKE 'BT-LIMIT-%' OR run_id LIKE 'BT-RESULT-%' OR run_id LIKE 'BT-TRADE-%' OR run_id LIKE 'BT-CUSTOM-%'")
        conn.execute("DELETE FROM strategies WHERE strategy_id = 'test_custom_999'")


@pytest.fixture
def sample_config():
    """标准回测配置"""
    return {
        "code": "600519",
        "stock_name": "贵州茅台",
        "strategy_id": "dual_ma",
        "strategy_name": "DualMAStrategy",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "initial_cash": 100000,
        "params": {"fast": 5, "slow": 20},
    }


# ── 策略 CRUD ──────────────────────────────────────────────

class TestStrategies:
    """策略定义 CRUD"""

    def test_builtin_strategies_registered(self):
        """内置策略已注册"""
        strategies = get_strategies()
        ids = {s["strategy_id"] for s in strategies}
        assert "dual_ma" in ids
        assert "macd_cross" in ids
        assert "boll_breakout" in ids
        assert "rsi_reversal" in ids

    def test_get_strategy_by_id(self):
        """按 ID 获取策略"""
        s = get_strategy("dual_ma")
        assert s is not None
        assert s["name"] == "双均线交叉"
        assert s["category"] == "trend"

    def test_get_strategy_not_found(self):
        """不存在的策略返回 None"""
        assert get_strategy("nonexistent_999") is None

    def test_filter_by_category(self):
        """按分类过滤"""
        trend = get_strategies(category="trend")
        assert all(s["category"] == "trend" for s in trend)
        assert len(trend) >= 2

    def test_upsert_custom_strategy(self):
        """插入自定义策略"""
        upsert_strategy(
            strategy_id="test_custom_999",
            name="测试策略",
            description="集成测试用",
            category="test",
            params_schema=json.dumps([{"key": "period", "type": "int", "default": 10}]),
        )
        s = get_strategy("test_custom_999")
        assert s is not None
        assert s["name"] == "测试策略"
        assert s["is_builtin"] == 0


# ── 回测任务 CRUD ──────────────────────────────────────────

class TestBacktestRuns:
    """回测任务 CRUD"""

    def test_create_and_get_run(self, sample_config):
        """创建并读取回测任务"""
        run_id = "BT-TEST-001"
        run = create_backtest_run(run_id, sample_config)
        assert run is not None
        assert run["run_id"] == run_id
        assert run["code"] == "600519"
        assert run["status"] == "pending"

    def test_run_preserves_config(self, sample_config):
        """任务保存完整配置"""
        run_id = "BT-TEST-002"
        create_backtest_run(run_id, sample_config)
        run = get_backtest_run(run_id)
        config = json.loads(run["config_json"])
        assert config["code"] == "600519"
        assert config["strategy_id"] == "dual_ma"

    def test_update_run_status(self, sample_config):
        """更新任务状态"""
        run_id = "BT-TEST-003"
        create_backtest_run(run_id, sample_config)
        update_backtest_run(run_id, status="running")
        run = get_backtest_run(run_id)
        assert run["status"] == "running"

    def test_update_run_completion(self, sample_config):
        """更新任务完成"""
        run_id = "BT-TEST-004"
        create_backtest_run(run_id, sample_config)
        update_backtest_run(
            run_id,
            status="completed",
            completed_at="2025-06-01 10:00:00",
            duration_seconds=5.5,
        )
        run = get_backtest_run(run_id)
        assert run["status"] == "completed"
        assert run["duration_seconds"] == 5.5

    def test_list_runs_by_code(self, sample_config):
        """按股票代码过滤"""
        run_id = "BT-TEST-005"
        create_backtest_run(run_id, sample_config)
        runs = get_backtest_runs(code="600519")
        assert len(runs) >= 1
        assert all(r["code"] == "600519" for r in runs)

    def test_list_runs_with_limit(self, sample_config):
        """分页查询"""
        for i in range(3):
            create_backtest_run(f"BT-LIMIT-{i}", sample_config)
        runs = get_backtest_runs(limit=2)
        assert len(runs) <= 2

    def test_get_nonexistent_run(self):
        """不存在的任务返回 None"""
        assert get_backtest_run("BT-NONEXISTENT-999") is None


# ── 回测结果 CRUD ──────────────────────────────────────────

class TestBacktestResults:
    """回测结果 CRUD"""

    def test_save_and_get_result(self, sample_config):
        """保存并读取结果"""
        run_id = "BT-RESULT-001"
        create_backtest_run(run_id, sample_config)

        result_data = {
            "total_return": 0.15,
            "annual_return": 0.12,
            "sharpe_ratio": 1.5,
            "max_drawdown": -0.08,
            "trade_count": 20,
            "win_count": 12,
            "loss_count": 8,
            "win_rate": 0.6,
            "profit_factor": 1.8,
            "final_equity": 115000.0,
            "peak_equity": 118000.0,
            "equity_curve_json": json.dumps([{"date": "2025-01-01", "equity": 100000}]),
            "drawdown_curve_json": json.dumps([{"date": "2025-01-01", "drawdown": 0}]),
            "monthly_returns_json": json.dumps({"2025": [1.5, -0.5, 2.0] + [0] * 9}),
        }
        save_backtest_result(run_id, result_data)

        result = get_backtest_result(run_id)
        assert result is not None
        assert result["total_return"] == 0.15
        assert result["sharpe_ratio"] == 1.5
        assert result["trade_count"] == 20

    def test_result_json_fields(self, sample_config):
        """JSON 字段正确存储"""
        run_id = "BT-RESULT-002"
        create_backtest_run(run_id, sample_config)

        curve = [{"date": "2025-01-01", "equity": 100000}, {"date": "2025-01-02", "equity": 101000}]
        save_backtest_result(run_id, {
            "equity_curve_json": json.dumps(curve),
            "drawdown_curve_json": json.dumps([]),
            "monthly_returns_json": json.dumps({}),
        })

        result = get_backtest_result(run_id)
        parsed = json.loads(result["equity_curve_json"])
        assert len(parsed) == 2
        assert parsed[0]["equity"] == 100000

    def test_overwrite_result(self, sample_config):
        """重复保存覆盖旧结果"""
        run_id = "BT-RESULT-003"
        create_backtest_run(run_id, sample_config)

        save_backtest_result(run_id, {"total_return": 0.10, "equity_curve_json": "[]",
                                       "drawdown_curve_json": "[]", "monthly_returns_json": "{}"})
        save_backtest_result(run_id, {"total_return": 0.25, "equity_curve_json": "[]",
                                       "drawdown_curve_json": "[]", "monthly_returns_json": "{}"})

        result = get_backtest_result(run_id)
        assert result["total_return"] == 0.25


# ── 交易记录 CRUD ──────────────────────────────────────────

class TestBacktestTrades:
    """交易记录 CRUD"""

    def test_save_and_get_trades(self, sample_config):
        """保存并读取交易记录"""
        run_id = "BT-TRADE-001"
        create_backtest_run(run_id, sample_config)

        trades = [
            {
                "trade_no": 1,
                "direction": "buy",
                "trade_date": "2025-03-15",
                "price": 1800.0,
                "quantity": 100,
                "amount": 180000.0,
                "commission": 54.0,
                "signal_reason": "MA5 上穿 MA20",
            },
            {
                "trade_no": 2,
                "direction": "sell",
                "trade_date": "2025-06-20",
                "price": 1950.0,
                "quantity": 100,
                "amount": 195000.0,
                "commission": 58.5,
                "pnl": 14947.5,
                "pnl_pct": 0.083,
                "holding_days": 97,
                "signal_reason": "MA5 下穿 MA20",
            },
        ]
        save_backtest_trades(run_id, trades)

        saved = get_backtest_trades(run_id)
        assert len(saved) == 2
        assert saved[0]["direction"] == "buy"
        assert saved[1]["pnl"] == 14947.5
        assert saved[1]["holding_days"] == 97

    def test_trades_ordered_by_no(self, sample_config):
        """交易按序号排列"""
        run_id = "BT-TRADE-002"
        create_backtest_run(run_id, sample_config)

        trades = [
            {"trade_no": 3, "direction": "sell", "trade_date": "2025-06-01", "price": 1900, "quantity": 100, "amount": 190000},
            {"trade_no": 1, "direction": "buy", "trade_date": "2025-01-01", "price": 1800, "quantity": 100, "amount": 180000},
            {"trade_no": 2, "direction": "buy", "trade_date": "2025-03-01", "price": 1850, "quantity": 100, "amount": 185000},
        ]
        save_backtest_trades(run_id, trades)

        saved = get_backtest_trades(run_id)
        assert [t["trade_no"] for t in saved] == [1, 2, 3]

    def test_empty_trades(self, sample_config):
        """空交易列表"""
        run_id = "BT-TRADE-003"
        create_backtest_run(run_id, sample_config)
        save_backtest_trades(run_id, [])
        assert get_backtest_trades(run_id) == []


# ── 统计 ────────────────────────────────────────────────────

class TestDataStats:
    """数据统计"""

    def test_stats_has_all_tables(self):
        """统计包含所有表"""
        stats = get_data_stats()
        assert "strategies" in stats
        assert "backtest_runs" in stats
        assert "backtest_results" in stats
        assert "backtest_trades" in stats
        assert all(isinstance(v, int) for v in stats.values())

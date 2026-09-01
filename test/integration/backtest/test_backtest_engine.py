"""回测引擎集成测试

测试 BacktestEngine 完整流程：
- 加载数据 → 配置策略 → 运行回测 → 提取结果
- 使用真实数据（从 market_data.db 读取，不足时自动补拉）
"""
import pytest
import json

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(120),
]


@pytest.fixture(scope="module")
def _ensure_test_data():
    """确保测试用股票有足够数据

    尝试从 akshare 补拉数据到 market_data.db。
    网络不可用时跳过整个模块。
    """
    from utils.cache.market_data_db import get_stock_daily_count, fetch_and_store_daily

    code = "600519"
    count = get_stock_daily_count(code)
    if count >= 20:
        return

    try:
        fetched = fetch_and_store_daily(code, days=250)
        if fetched < 20:
            pytest.skip(f"数据不足: {code} 只拉到 {fetched} 条")
    except Exception as e:
        pytest.skip(f"无法获取测试数据: {e}")


@pytest.fixture
def base_config(_ensure_test_data):
    """基础回测配置，日期自动匹配 DB 中已有数据"""
    from utils.cache.market_data_db import get_stock_daily
    df = get_stock_daily("600519")
    start = df.iloc[0]["trade_date"]
    end = df.iloc[-1]["trade_date"]
    return {
        "code": "600519",
        "stock_name": "贵州茅台",
        "strategy_id": "dual_ma",
        "start_date": start,
        "end_date": end,
        "initial_cash": 200000,
        "params": {"fast": 5, "slow": 20},
        "t_plus_1": True,
        "lot_size": 100,
    }


# ── 基本回测运行 ────────────────────────────────────────────

class TestBacktestEngineRun:
    """BacktestEngine.run 完整流程"""

    def test_returns_completed_result(self, base_config):
        """运行成功返回 completed 状态"""
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()
        result = engine.run(base_config)

        assert result["status"] == "completed"
        assert "run_id" in result
        assert result["run_id"].startswith("BT-")

    def test_result_has_required_fields(self, base_config):
        """结果包含必要统计字段"""
        from backtest.engine import BacktestEngine
        result = BacktestEngine().run(base_config)

        required = {
            "total_return", "annual_return", "sharpe_ratio", "max_drawdown",
            "trade_count", "win_rate", "final_equity", "equity_curve",
            "drawdown_curve", "monthly_returns",
        }
        assert required <= set(result.keys())

    def test_equity_curve_structure(self, base_config):
        """净值曲线结构正确"""
        from backtest.engine import BacktestEngine
        result = BacktestEngine().run(base_config)

        curve = result["equity_curve"]
        assert isinstance(curve, list)
        if curve:
            assert "date" in curve[0]
            assert "equity" in curve[0]
            assert isinstance(curve[0]["equity"], (int, float))

    def test_monthly_returns_structure(self, base_config):
        """月度收益结构正确"""
        from backtest.engine import BacktestEngine
        result = BacktestEngine().run(base_config)

        monthly = result["monthly_returns"]
        assert isinstance(monthly, dict)
        for year, values in monthly.items():
            assert len(values) == 12

    def test_custom_run_id(self, base_config):
        """支持自定义 run_id"""
        from backtest.engine import BacktestEngine
        custom_id = "BT-CUSTOM-TEST-999"
        result = BacktestEngine().run(base_config, run_id=custom_id)
        assert result["run_id"] == custom_id

    def test_result_saved_to_db(self, base_config):
        """结果已保存到数据库"""
        from backtest.engine import BacktestEngine
        from utils.cache.backtest_db import get_backtest_result, get_backtest_trades

        result = BacktestEngine().run(base_config)
        run_id = result["run_id"]

        db_result = get_backtest_result(run_id)
        assert db_result is not None
        assert db_result["total_return"] == result["total_return"]


# ── 策略参数 ────────────────────────────────────────────────

class TestStrategyParams:
    """不同策略参数"""

    def test_different_ma_periods(self, base_config):
        """不同均线周期"""
        from backtest.engine import BacktestEngine
        base_config["params"] = {"fast": 3, "slow": 10}
        result = BacktestEngine().run(base_config)
        assert result["status"] == "completed"

    def test_with_stop_loss(self, base_config):
        """带止损"""
        from backtest.engine import BacktestEngine
        base_config["stop_loss"] = 0.05
        result = BacktestEngine().run(base_config)
        assert result["status"] == "completed"

    def test_with_take_profit(self, base_config):
        """带止盈"""
        from backtest.engine import BacktestEngine
        base_config["take_profit"] = 0.10
        result = BacktestEngine().run(base_config)
        assert result["status"] == "completed"


# ── 错误处理 ────────────────────────────────────────────────

class TestBacktestErrors:
    """错误场景"""

    def test_invalid_strategy(self, base_config):
        """无效策略抛异常"""
        from backtest.engine import BacktestEngine
        base_config["strategy_id"] = "nonexistent_strategy_999"
        with pytest.raises(Exception):
            BacktestEngine().run(base_config)

    def test_insufficient_cash(self, base_config):
        """资金不足抛异常"""
        from backtest.engine import BacktestEngine
        base_config["initial_cash"] = 100  # 远不够买1手
        with pytest.raises(ValueError, match="资金不足"):
            BacktestEngine().run(base_config)

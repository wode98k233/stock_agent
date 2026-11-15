"""引擎级执行归因集成测试（P0 §4.6）。

引擎真跑（临时库），行情走 data_adapter 打桩、策略用受控测试类——
验证「拒单 → 归因文案 → warnings（实时）+ health_detail（落库）」全链路，
以及口径声明常显。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """把回测库指向临时文件并建表（范式同 test_entry_timing.py）"""
    test_db = str(tmp_path / "backtest.db")
    with patch("config.Config.get_backtest_db_path", return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _base_config(**overrides) -> dict:
    config = {
        "code": "600519", "strategy_id": "attr_probe", "params": {},
        "start_date": "2025-01-02", "end_date": "2025-01-08",
        "initial_cash": 200000, "commission": 0.0003, "min_commission": 5.0,
        "stamp_tax": 0.0005, "transfer_fee": 0.00001, "slippage": 0.001,
        "t_plus_1": True, "lot_size": 100, "adjust_type": "qfq",
        "stop_loss": 0, "take_profit": 0, "trailing_stop": 0,
        "position_mode": "full", "position_size": 1.0, "asset_type": "stock",
    }
    config.update(overrides)
    return config


def _limit_up_frame() -> pd.DataFrame:
    """5 根 bar：bar3 一字涨停（+10%）——bar2 的买单在 bar3 被拒、bar4 作废。"""
    rows = [
        ('2025-01-02', 10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        ('2025-01-03', 10.0, 10.1, 9.9, 10.0, 100000, 10.0),
        ('2025-01-06', 11.0, 11.0, 11.0, 11.0, 100000, 10.0),   # 一字涨停
        ('2025-01-07', 11.0, 11.1, 10.9, 11.0, 100000, 11.0),
        ('2025-01-08', 11.0, 11.1, 10.9, 11.0, 100000, 11.0),
    ]
    return pd.DataFrame([{
        'trade_date': r[0], 'open': r[1], 'high': r[2], 'low': r[3],
        'close': r[4], 'volume': r[5], 'pre_close': r[6],
    } for r in rows])


def _probe_strategy():
    """第 2 根 bar 买入一次的受控策略（继承项目基类，走 buy_with_reason 正路）"""
    from backtest.strategy_base import StockSolveStrategy

    class _AttrProbe(StockSolveStrategy):
        def on_bar(self):
            if len(self) == 2 and not self.position and not self.order:
                self.buy_with_reason('集成测试买入')

    return _AttrProbe


def test_engine_reports_execution_attribution(setup_test_env):
    from backtest.engine import BacktestEngine

    with patch('backtest.data_adapter._load_stock_daily', return_value=_limit_up_frame()), \
         patch('backtest.engine.get_strategy_class', return_value=_probe_strategy()), \
         patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        result = BacktestEngine().run(_base_config(), run_id='BT-ATTR-0001')

    joined = '\n'.join(result['warnings'])
    # 归因（实时响应落点）：1 次买入尝试、1 次因涨停被拒（主板 10% 标签）
    assert '触发 1 次买入信号，1 次因涨停未成交（主板 10% 限制）' in joined
    assert '1 笔委托当日未成交已自动作废' in joined
    # 口径声明常显（§11.8.3）
    assert '数据口径：前复权（源侧预复权）' in joined

    # 归因落库（刷新后可见落点）：health_detail
    con = sqlite3.connect(setup_test_env)
    try:
        row = con.execute(
            'SELECT health_detail FROM backtest_results WHERE run_id = ?',
            ('BT-ATTR-0001',)).fetchone()
    finally:
        con.close()
    assert row is not None, '回测结果应已落库'
    assert '1 次因涨停未成交' in (row[0] or '')


def test_engine_records_price_limit_mode_snapshot(setup_test_env):
    """账本口径快照：run 写入 price_limit_mode + engine_version v1.1（§4.7）"""
    from backtest.engine import BacktestEngine

    with patch('backtest.data_adapter._load_stock_daily', return_value=_limit_up_frame()), \
         patch('backtest.engine.get_strategy_class', return_value=_probe_strategy()), \
         patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        BacktestEngine().run(_base_config(), run_id='BT-ATTR-0002')

    con = sqlite3.connect(setup_test_env)
    try:
        row = con.execute(
            'SELECT price_limit_mode, engine_version FROM backtest_runs WHERE run_id = ?',
            ('BT-ATTR-0002',)).fetchone()
    finally:
        con.close()
    assert row[0] == 'enforce', '默认口径应为 enforce（全局配置默认）'
    assert row[1] == 'v1.1', 'enforce 口径的 run 应记 engine_version=v1.1'


def test_engine_off_mode_records_off(setup_test_env):
    """off 口径随请求透传：run 快照 + 无一笔拒单"""
    from backtest.engine import BacktestEngine

    with patch('backtest.data_adapter._load_stock_daily', return_value=_limit_up_frame()), \
         patch('backtest.engine.get_strategy_class', return_value=_probe_strategy()), \
         patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        result = BacktestEngine().run(
            _base_config(price_limit_mode='off'), run_id='BT-ATTR-0003')

    joined = '\n'.join(result['warnings'])
    assert '因涨停未成交' not in joined, 'off 档不允许出现拦截归因'

    con = sqlite3.connect(setup_test_env)
    try:
        row = con.execute(
            'SELECT price_limit_mode, engine_version FROM backtest_runs WHERE run_id = ?',
            ('BT-ATTR-0003',)).fetchone()
    finally:
        con.close()
    assert row[0] == 'off'
    assert row[1] == 'v1.0', '非 enforce 口径保持 engine_version=v1.0'

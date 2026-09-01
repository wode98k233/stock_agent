"""backtest.db 数据库层测试"""
import pytest
import json
import os
import tempfile
from unittest.mock import patch


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """使用临时数据库"""
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def test_init_tables(setup_test_db):
    """测试表初始化"""
    import sqlite3
    conn = sqlite3.connect(setup_test_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert 'data_tasks' in tables
    assert 'strategies' in tables
    assert 'backtest_runs' in tables
    assert 'backtest_results' in tables
    assert 'backtest_trades' in tables


def test_builtin_strategies_registered(setup_test_db):
    """测试内置策略注册"""
    from utils.cache.backtest_db import get_strategies
    strategies = get_strategies()
    ids = {s['strategy_id'] for s in strategies}
    assert 'dual_ma_crossover' in ids
    assert 'macd_crossover' in ids
    assert 'rsi_overbought_oversold' in ids


def test_get_strategy(setup_test_db):
    """测试获取单个策略"""
    from utils.cache.backtest_db import get_strategy
    s = get_strategy('dual_ma_crossover')
    assert s is not None
    assert s['name'] == '双均线金叉'
    assert s['category'] == 'trend'
    assert s['is_builtin'] == 1


def test_create_data_task(setup_test_db):
    """测试创建采集任务"""
    from utils.cache.backtest_db import create_data_task, get_data_task
    create_data_task('T-TEST-001', 'stock_list', '{"source": "akshare"}')
    task = get_data_task('T-TEST-001')
    assert task is not None
    assert task['task_type'] == 'stock_list'
    assert task['status'] == 'pending'


def test_update_data_task(setup_test_db):
    """测试更新采集任务"""
    from utils.cache.backtest_db import create_data_task, update_data_task, get_data_task
    create_data_task('T-TEST-002', 'daily_kline', '{}')
    update_data_task('T-TEST-002', status='running', total_count=100, processed_count=50)
    task = get_data_task('T-TEST-002')
    assert task['status'] == 'running'
    assert task['total_count'] == 100
    assert task['processed_count'] == 50


def test_append_task_log(setup_test_db):
    """测试追加日志"""
    from utils.cache.backtest_db import create_data_task, append_task_log, get_data_task
    create_data_task('T-TEST-003', 'stock_list', '{}')
    append_task_log('T-TEST-003', '[INFO] 开始采集')
    append_task_log('T-TEST-003', '[OK] 完成')
    task = get_data_task('T-TEST-003')
    assert '[INFO] 开始采集' in task['log_text']
    assert '[OK] 完成' in task['log_text']


def test_backtest_run_crud(setup_test_db):
    """测试回测任务 CRUD"""
    from utils.cache.backtest_db import create_backtest_run, get_backtest_run, update_backtest_run
    config = {
        'code': '600519', 'strategy_id': 'dual_ma_crossover', 'strategy_name': '双均线',
        'params': {'fast': 5, 'slow': 20}, 'start_date': '2025-01-01', 'end_date': '2026-01-01',
    }
    create_backtest_run('BT-TEST-001', config)
    run = get_backtest_run('BT-TEST-001')
    assert run is not None
    assert run['code'] == '600519'
    assert run['status'] == 'pending'

    update_backtest_run('BT-TEST-001', status='completed')
    run = get_backtest_run('BT-TEST-001')
    assert run['status'] == 'completed'


def test_backtest_result_crud(setup_test_db):
    """测试回测结果 CRUD"""
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result, get_backtest_result
    config = {'code': '600519', 'strategy_id': 'dual_ma_crossover', 'strategy_name': 'test',
              'params': {}, 'start_date': '2025-01-01', 'end_date': '2026-01-01'}
    create_backtest_run('BT-TEST-002', config)
    save_backtest_result('BT-TEST-002', {
        'total_return': 0.15, 'annual_return': 0.12, 'sharpe_ratio': 1.5,
        'max_drawdown': 0.08, 'trade_count': 10, 'win_rate': 0.6,
    })
    result = get_backtest_result('BT-TEST-002')
    assert result is not None
    assert result['total_return'] == 0.15
    assert result['trade_count'] == 10


def test_backtest_trades_crud(setup_test_db):
    """测试交易记录 CRUD"""
    from utils.cache.backtest_db import create_backtest_run, save_backtest_trades, get_backtest_trades
    config = {'code': '600519', 'strategy_id': 'dual_ma_crossover', 'strategy_name': 'test',
              'params': {}, 'start_date': '2025-01-01', 'end_date': '2026-01-01'}
    create_backtest_run('BT-TEST-003', config)
    save_backtest_trades('BT-TEST-003', [
        {'trade_no': 1, 'direction': 'buy', 'trade_date': '2025-01-15', 'price': 100, 'quantity': 100, 'amount': 10000},
        {'trade_no': 2, 'direction': 'sell', 'trade_date': '2025-02-20', 'price': 110, 'quantity': 100, 'amount': 11000, 'pnl': 900},
    ])
    trades = get_backtest_trades('BT-TEST-003')
    assert len(trades) == 2
    assert trades[0]['direction'] == 'buy'
    assert trades[1]['pnl'] == 900

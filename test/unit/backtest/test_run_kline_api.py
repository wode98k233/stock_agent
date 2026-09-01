"""Test: build_run_kline 数据组装（mock DB + 复权 + 策略声明）"""
import pytest
from unittest.mock import patch
import pandas as pd


def _fake_df():
    return pd.DataFrame({
        'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
        'open': [10.0, 10.5, 11.0], 'high': [10.8, 11.2, 11.5],
        'low': [9.9, 10.3, 10.8], 'close': [10.5, 11.0, 11.3],
        'volume': [1000, 1200, 900], 'amount': [1e6, 1.1e6, 9e5],
    })


def _patches(run, trades, df):
    return [
        patch('server.routes.backtest.get_backtest_run', return_value=run),
        patch('server.routes.backtest.get_backtest_trades', return_value=trades),
        patch('backtest.data_adapter._load_stock_daily', return_value=df),
    ]


def test_assembles_kline_trades_indicators():
    from server.routes.backtest import build_run_kline
    run = {
        'code': '600519', 'stock_name': '贵州茅台', 'adjust_type': 'none',
        'start_date': '2025-01-01', 'end_date': '2025-01-10',
        'strategy_id': 'dual_ma_crossover', 'params_json': '{"fast_period": 2, "slow_period": 3}',
    }
    trades = [
        {'trade_date': '2025-01-03', 'direction': 'buy', 'price': 11.0, 'quantity': 100,
         'pnl': None, 'pnl_pct': None, 'signal_reason': 'MA2 上穿 MA3'},
    ]
    ps = _patches(run, trades, _fake_df())
    for p in ps: p.start()
    try:
        data = build_run_kline('BT-X')
    finally:
        for p in ps: p.stop()

    assert data['code'] == '600519'
    assert data['name'] == '贵州茅台'
    assert len(data['kline']) == 3
    assert data['kline'][0] == {'date': '2025-01-02', 'open': 10.0,
        'high': 10.8, 'low': 9.9, 'close': 10.5, 'volume': 1000}
    assert data['trades'][0]['date'] == '2025-01-03'
    assert data['trades'][0]['direction'] == 'buy'
    assert [s['name'] for s in data['indicators']['main']] == ['MA2', 'MA3']


def test_run_not_found():
    from server.routes.backtest import build_run_kline
    with patch('server.routes.backtest.get_backtest_run', return_value=None):
        assert build_run_kline('NOPE') is None


def test_qfq_calls_apply_adjustment():
    from server.routes.backtest import build_run_kline
    run = {
        'code': '600519', 'stock_name': '', 'adjust_type': 'qfq',
        'start_date': '2025-01-01', 'end_date': '2025-01-10',
        'strategy_id': 'dual_ma_crossover', 'params_json': '{}',
    }
    ps = _patches(run, [], _fake_df())
    with patch('backtest.data_adapter._apply_adjustment', return_value=_fake_df()) as adj:
        for p in ps: p.start()
        try:
            build_run_kline('BT-X')
        finally:
            for p in ps: p.stop()
        adj.assert_called_once()


def test_empty_kline_no_crash():
    from server.routes.backtest import build_run_kline
    run = {
        'code': 'X', 'stock_name': '', 'adjust_type': 'none',
        'start_date': '2025-01-01', 'end_date': '2025-01-10',
        'strategy_id': 'dual_ma_crossover', 'params_json': '{}',
    }
    ps = _patches(run, [], pd.DataFrame())
    for p in ps: p.start()
    try:
        data = build_run_kline('BT-X')
    finally:
        for p in ps: p.stop()
    assert data['kline'] == []
    assert data['indicators'] == {'main': [], 'sub': []}

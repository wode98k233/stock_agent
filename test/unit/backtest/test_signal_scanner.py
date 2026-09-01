"""SignalScanner 单元测试

覆盖：
- on_bar scan 模式：记录信号、无交易、无状态评估
- on_bar 默认模式不变（回归）
- SignalScanner.scan() 结构、latest_is_buy、空数据
"""
import os
import sys
import json
from unittest.mock import patch

import pytest
import backtrader as bt
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.json_compiler import compile_strategy


# ============================================================
# 数据与运行辅助
# ============================================================

def _synthetic_df(n=120, seed=0, base=10.0):
    """合成 OHLCV 随机走势，保证 low <= close <= high"""
    rng = np.random.RandomState(seed)
    rets = rng.normal(0, 0.02, n)
    close = base * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.015, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.015, n)))
    high = np.maximum(high, close)
    low = np.minimum(low, close)
    open_ = (high + low) / 2
    volume = rng.randint(1000, 50000, n).astype(float)
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates,
    )


def _crossover_df(n=90):
    """构造含金叉/死叉的 DataFrame：先跌→涨→跌，确保交叉发生在 MA20 warmup 之后

    open = 前一根 close，避免 backtrader 次一根 open 跳价导致 full 仓位 margin 拒单。
    """
    third = n // 3
    close = np.concatenate([
        np.linspace(20, 10, third),        # 下跌
        np.linspace(10, 25, third),        # 反弹 → 金叉
        np.linspace(25, 12, n - 2 * third), # 回落 → 死叉
    ])
    high = close + 0.5
    low = close - 0.5
    open_ = np.concatenate([[close[0]], close[:-1]])  # open = 前一根 close
    volume = np.full(n, 10000.0)
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates,
    )


def _run_strategy(cls, df, **kwargs):
    """跑策略，返回策略实例"""
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(cls, **kwargs)
    cerebro.broker.setcash(1_000_000)
    results = cerebro.run()
    return results[0]


def _to_db_df(df):
    """将 DatetimeIndex DataFrame 转为含 trade_date 列的 DataFrame（模拟 DB 查询结果）"""
    result = df.copy()
    result['trade_date'] = result.index.strftime('%Y-%m-%d')
    result = result.reset_index(drop=True)
    result['code'] = '600519'
    result['amount'] = result['close'] * result['volume']
    return result


def _dual_ma_json():
    """标准 dual_ma 金叉死叉策略"""
    return json.dumps({
        'meta': {'name': 'DualMA', 'strategy_id': 'dual_ma_test'},
        'params': {'fast': {'value': 5}, 'slow': {'value': 20}},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator',
                 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_above',
                 'right': {'func': 'ma', 'args': ['close', '{slow}']}},
            ]},
            'sell': {'logic': 'AND', 'rules': [
                {'type': 'indicator',
                 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_below',
                 'right': {'func': 'ma', 'args': ['close', '{slow}']}},
            ]},
        },
    })


def _always_buy_json():
    """买入条件恒为真：close > 0"""
    return json.dumps({
        'meta': {'name': 'AlwaysBuy', 'strategy_id': 'always_buy'},
        'params': {},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'price', 'left': {'data': 'close'}, 'op': '>', 'right': {'value': 0}},
            ]},
            'sell': {'logic': 'AND', 'rules': []},
        },
    })


# ============================================================
# on_bar scan 模式测试
# ============================================================

class TestScanModeOnBar:
    """on_bar scan 模式行为测试"""

    def test_scan_mode_logs_signals(self):
        """scan 模式正确记录金叉信号"""
        cls = compile_strategy(_dual_ma_json())
        df = _crossover_df(n=60)
        strat = _run_strategy(cls, df, _scan_mode=True)

        signals = strat.get_signal_log()
        assert len(signals) > 0
        buy_signals = [s for s in signals if s['buy']]
        assert len(buy_signals) > 0
        # 信号格式正确
        assert 'date' in signals[0]
        assert 'buy' in signals[0]
        assert 'sell' in signals[0]

    def test_scan_mode_no_trades(self):
        """scan 模式不产生交易"""
        cls = compile_strategy(_dual_ma_json())
        df = _crossover_df(n=60)
        strat = _run_strategy(cls, df, _scan_mode=True)

        assert strat.get_trades() == []

    def test_scan_mode_evaluates_buy_every_bar(self):
        """scan 模式无状态评估：buy 条件恒真时每根 bar 都记 buy=True"""
        cls = compile_strategy(_always_buy_json())
        df = _synthetic_df(n=30, seed=42)
        strat = _run_strategy(cls, df, _scan_mode=True)

        signals = strat.get_signal_log()
        # 每根 bar 都命中 buy（无状态评估，不受持仓影响）
        assert len(signals) == 30
        assert all(s['buy'] for s in signals)

    def test_scan_mode_sell_tree_safe_without_position(self):
        """sell 含 CrossOver，scan 全程无持仓评估不抛异常"""
        cls = compile_strategy(_dual_ma_json())
        df = _crossover_df(n=60)
        # 不抛异常即通过
        strat = _run_strategy(cls, df, _scan_mode=True)
        signals = strat.get_signal_log()
        # 应该同时有 buy 和 sell 信号
        assert any(s['buy'] for s in signals)
        assert any(s['sell'] for s in signals)

    def test_empty_sell_tree_scan_safe(self):
        """sell rules 空的策略 scan 不报错"""
        cls = compile_strategy(_always_buy_json())
        df = _synthetic_df(n=20, seed=1)
        strat = _run_strategy(cls, df, _scan_mode=True)
        signals = strat.get_signal_log()
        # 空 sell_tree → sell 永远 False，只有 buy 信号
        assert all(not s['sell'] for s in signals)


class TestDefaultModeUnchanged:
    """默认模式不变性回归测试"""

    def test_default_mode_signal_log_empty(self):
        """默认模式 _signal_log 为空"""
        cls = compile_strategy(_dual_ma_json())
        df = _crossover_df(n=60)
        strat = _run_strategy(cls, df)  # 不传 _scan_mode

        assert strat.get_signal_log() == []

    def test_default_mode_produces_trades(self):
        """默认模式正常产生交易（与改动前一致）"""
        cls = compile_strategy(_dual_ma_json())
        df = _crossover_df(n=60)
        strat = _run_strategy(cls, df)

        trades = strat.get_trades()
        assert len(trades) > 0
        assert any(t['direction'] == 'buy' for t in trades)


# ============================================================
# SignalScanner.scan() 测试
# ============================================================

class TestSignalScannerScan:
    """SignalScanner.scan() 测试（mock DB 层）"""

    def test_scan_returns_correct_structure(self):
        """scan() 返回完整结构"""
        from backtest.signal_scanner import SignalScanner

        cls = compile_strategy(_dual_ma_json())
        mock_df = _to_db_df(_crossover_df(n=60))

        with patch('backtest.signal_scanner._load_stock_daily', return_value=mock_df), \
             patch('backtest.signal_scanner._apply_adjustment', return_value=mock_df), \
             patch('backtest.signal_scanner.get_strategy_class', return_value=cls):
            scanner = SignalScanner()
            result = scanner.scan(
                code='600519', strategy_id='dual_ma_test',
                start_date='2025-01-01', end_date='2025-12-31',
            )

        assert result['code'] == '600519'
        assert result['strategy_id'] == 'dual_ma_test'
        assert len(result['kline']) > 0
        assert 'signals' in result
        assert 'latest_is_buy' in result
        assert 'latest_is_sell' in result
        assert 'latest_date' in result
        assert 'indicators' in result
        # kline 格式正确
        k = result['kline'][0]
        assert all(key in k for key in ['date', 'open', 'high', 'low', 'close', 'volume'])

    def test_scan_latest_is_buy_detection(self):
        """latest_is_buy 正确检测最新 bar 是否买点"""
        from backtest.signal_scanner import SignalScanner

        # 用 always_buy 策略：每根 bar 都是买点
        cls = compile_strategy(_always_buy_json())
        mock_df = _to_db_df(_synthetic_df(n=30, seed=5))

        with patch('backtest.signal_scanner._load_stock_daily', return_value=mock_df), \
             patch('backtest.signal_scanner._apply_adjustment', return_value=mock_df), \
             patch('backtest.signal_scanner.get_strategy_class', return_value=cls):
            scanner = SignalScanner()
            result = scanner.scan(
                code='600519', strategy_id='always_buy',
                start_date='2025-01-01', end_date='2025-12-31',
            )

        assert result['latest_is_buy'] is True
        assert result['latest_date'] == result['kline'][-1]['date']

    def test_scan_empty_data(self):
        """空数据返回空结果"""
        from backtest.signal_scanner import SignalScanner

        cls = compile_strategy(_dual_ma_json())
        with patch('backtest.signal_scanner._load_stock_daily', return_value=pd.DataFrame()), \
             patch('backtest.signal_scanner.get_strategy_class', return_value=cls):
            scanner = SignalScanner()
            result = scanner.scan(
                code='000000', strategy_id='dual_ma_test',
                start_date='2025-01-01', end_date='2025-12-31',
            )

        assert result['kline'] == []
        assert result['signals'] == []
        assert result['latest_is_buy'] is False
        assert result['latest_date'] == ''

    def test_scan_no_adjust_when_none(self):
        """adjust_type='none' 时不调用 _apply_adjustment"""
        from backtest.signal_scanner import SignalScanner

        cls = compile_strategy(_dual_ma_json())
        mock_df = _to_db_df(_synthetic_df(n=30, seed=3))

        with patch('backtest.signal_scanner._load_stock_daily', return_value=mock_df), \
             patch('backtest.signal_scanner._apply_adjustment') as mock_adj, \
             patch('backtest.signal_scanner.get_strategy_class', return_value=cls):
            scanner = SignalScanner()
            scanner.scan(
                code='600519', strategy_id='dual_ma_test',
                start_date='2025-01-01', end_date='2025-12-31',
                adjust_type='none',
            )

        mock_adj.assert_not_called()

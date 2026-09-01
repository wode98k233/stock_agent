"""Test: 策略 get_chart_indicators 声明 + 接口指标构建"""
import pytest
import pandas as pd
import numpy as np


# ── get_chart_indicators 声明 ──

def test_base_default_empty():
    from backtest.strategy_base import StockSolveStrategy
    assert StockSolveStrategy.get_chart_indicators({}) == []


def test_dual_ma():
    from backtest.strategies import get_strategy_class
    DualMAStrategy = get_strategy_class('dual_ma_crossover')
    decls = DualMAStrategy.get_chart_indicators({'fast_period': 8, 'slow_period': 30})
    assert decls == [
        {'type': 'MA', 'period': 8},
        {'type': 'MA', 'period': 30},
    ]


def test_dual_ma_defaults():
    from backtest.strategies import get_strategy_class
    DualMAStrategy = get_strategy_class('dual_ma_crossover')
    decls = DualMAStrategy.get_chart_indicators({})
    assert decls[0]['period'] == 5
    assert decls[1]['period'] == 20


def test_macd():
    from backtest.strategies import get_strategy_class
    MACDCrossStrategy = get_strategy_class('macd_crossover')
    decls = MACDCrossStrategy.get_chart_indicators({'fast': 10, 'slow': 20, 'signal': 5})
    assert decls == [
        {'type': 'MACD', 'fast': 10, 'slow': 20, 'signal': 5},
    ]


def test_rsi():
    from backtest.strategies import get_strategy_class
    RSIReversalStrategy = get_strategy_class('rsi_overbought_oversold')
    decls = RSIReversalStrategy.get_chart_indicators({'rsi_period': 9})
    assert decls == [{'type': 'RSI', 'period': 9}]


def test_boll():
    from backtest.strategies import get_strategy_class
    BollBreakoutStrategy = get_strategy_class('boll_breakout')
    decls = BollBreakoutStrategy.get_chart_indicators({'period': 25, 'std_dev': 2.5})
    # 模板中 BOLL chart_indicators 不含 panel 字段，返回解析后的参数
    assert len(decls) == 1
    assert decls[0]['type'] == 'BOLL'
    assert decls[0]['period'] == 25
    assert decls[0]['std'] == 2.5


# ── build_chart_indicators 构建 ──

@pytest.fixture
def sample_df(n=30):
    close = np.linspace(10, 20, n)
    return pd.DataFrame({
        'close': close, 'high': close + 0.5, 'low': close - 0.5,
    })


def test_ma_main(sample_df):
    from server.chart_utils import build_chart_indicators
    out = build_chart_indicators(sample_df, [
        {'type': 'MA', 'period': 5, 'panel': 'main'},
    ])
    assert len(out['main']) == 1
    assert out['main'][0]['name'] == 'MA5'
    assert len(out['main'][0]['data']) == 30
    assert out['main'][0]['data'][0] is None  # 前几日 NaN→None
    assert out['main'][0]['data'][-1] is not None


def test_boll_three_lines(sample_df):
    from server.chart_utils import build_chart_indicators
    out = build_chart_indicators(sample_df, [
        {'type': 'BOLL', 'period': 20, 'std': 2, 'panel': 'main'},
    ])
    names = [s['name'] for s in out['main']]
    assert names == ['BOLL上轨', 'BOLL中轨', 'BOLL下轨']


def test_macd_sub(sample_df):
    from server.chart_utils import build_chart_indicators
    out = build_chart_indicators(sample_df, [
        {'type': 'MACD', 'fast': 12, 'slow': 26, 'signal': 9, 'panel': 'sub'},
    ])
    assert out['sub'][0]['type'] == 'MACD'
    assert 'dif' in out['sub'][0]
    assert 'dea' in out['sub'][0]
    assert 'hist' in out['sub'][0]


def test_rsi_sub(sample_df):
    from server.chart_utils import build_chart_indicators
    out = build_chart_indicators(sample_df, [
        {'type': 'RSI', 'period': 14, 'panel': 'sub'},
    ])
    assert out['sub'][0]['type'] == 'RSI'
    assert len(out['sub'][0]['data']) == 30


def test_empty_df():
    from server.chart_utils import build_chart_indicators
    out = build_chart_indicators(pd.DataFrame(), [{'type': 'MA', 'period': 5, 'panel': 'main'}])
    assert out == {'main': [], 'sub': []}


def test_unknown_type_ignored(sample_df):
    from server.chart_utils import build_chart_indicators
    out = build_chart_indicators(sample_df, [{'type': 'FOO', 'panel': 'main'}])
    assert out == {'main': [], 'sub': []}

"""回测引擎测试"""
import pytest
import os
import tempfile
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """设置测试环境"""
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


@pytest.fixture
def mock_stock_daily():
    """模拟 stock_daily 数据"""
    dates = pd.date_range('2025-01-01', periods=200, freq='B')
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(200) * 2)
    df = pd.DataFrame({
        'trade_date': dates.strftime('%Y-%m-%d'),
        'open': prices + np.random.randn(200) * 0.5,
        'high': prices + abs(np.random.randn(200)) * 1.5,
        'low': prices - abs(np.random.randn(200)) * 1.5,
        'close': prices,
        'volume': np.random.randint(1000, 100000, 200).astype(float),
    })
    return df


def test_dual_ma_strategy(mock_stock_daily):
    """测试双均线策略"""
    import backtrader as bt
    from backtest.strategies import get_strategy_class
    DualMAStrategy = get_strategy_class('dual_ma_crossover')

    df = mock_stock_daily.copy()
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.set_index('trade_date')

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(DualMAStrategy, fast_period=5, slow_period=20)
    cerebro.broker.setcash(100000)
    results = cerebro.run()
    strategy = results[0]

    # 策略应该执行了
    assert hasattr(strategy, '_trades')


def test_macd_strategy(mock_stock_daily):
    """测试 MACD 策略"""
    import backtrader as bt
    from backtest.strategies import get_strategy_class
    MACDCrossStrategy = get_strategy_class('macd_crossover')

    df = mock_stock_daily.copy()
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.set_index('trade_date')

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(MACDCrossStrategy)
    cerebro.broker.setcash(100000)
    results = cerebro.run()
    assert results[0] is not None


def test_rsi_strategy(mock_stock_daily):
    """测试 RSI 策略"""
    import backtrader as bt
    from backtest.strategies import get_strategy_class
    RSIReversalStrategy = get_strategy_class('rsi_overbought_oversold')

    df = mock_stock_daily.copy()
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.set_index('trade_date')

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(RSIReversalStrategy)
    cerebro.broker.setcash(100000)
    results = cerebro.run()
    assert results[0] is not None


def test_a_stock_commission():
    """测试 A 股佣金计算"""
    from backtest.a_stock_broker import AStockCommission
    comm = AStockCommission(commission=0.0003, min_commission=5.0, stamp_tax=0.0005, transfer_fee=0.00001)

    # 买入 100 股 * 100 元 = 10000 元
    buy_cost = comm._getcommission(100, 100, None)
    # 佣金: 10000 * 0.0003 = 3, 但最低 5 元
    # 过户费: 10000 * 0.00001 = 0.1
    assert buy_cost >= 5.0  # 最低佣金

    # 卖出 100 股 * 100 元 = 10000 元
    sell_cost = comm._getcommission(-100, 100, None)
    # 佣金: max(3, 5) = 5
    # 印花税: 10000 * 0.0005 = 5
    # 过户费: 10000 * 0.00001 = 0.1
    assert sell_cost > buy_cost  # 卖出费用更高（有印花税）


def test_data_adapter_no_data():
    """测试数据适配器无数据时抛出异常"""
    from backtest.data_adapter import BacktraderDataFeed
    with patch('backtest.data_adapter._load_stock_daily', return_value=pd.DataFrame()):
        with pytest.raises(ValueError, match='无数据'):
            BacktraderDataFeed.from_stock_daily('999999', '2025-01-01', '2026-01-01')

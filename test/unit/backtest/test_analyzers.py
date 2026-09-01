"""回测分析器测试"""
import pytest
import backtrader as bt
import pandas as pd
import numpy as np
from backtest.analyzers import NeutralBandAnalyzer


@pytest.fixture
def mock_stock_data():
    """模拟股票数据"""
    dates = pd.date_range('2025-01-01', periods=100, freq='B')
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(100) * 2)
    df = pd.DataFrame({
        'open': prices + np.random.randn(100) * 0.5,
        'high': prices + abs(np.random.randn(100)) * 1.5,
        'low': prices - abs(np.random.randn(100)) * 1.5,
        'close': prices,
        'volume': np.random.randint(1000, 100000, 100).astype(float),
    }, index=dates)
    return df


class TestNeutralBandAnalyzer:
    """中性带宽分析器测试"""

    def test_analyzer_basic(self, mock_stock_data):
        """测试分析器基本功能"""
        from backtest.strategies import get_strategy_class
        DualMAStrategy = get_strategy_class('dual_ma_crossover')

        cerebro = bt.Cerebro()
        data = bt.feeds.PandasData(dataname=mock_stock_data)
        cerebro.adddata(data)
        cerebro.addstrategy(DualMAStrategy, fast_period=5, slow_period=20)
        cerebro.addanalyzer(NeutralBandAnalyzer, _name='neutral_band', neutral_band_pct=2.0)
        cerebro.broker.setcash(100000)
        results = cerebro.run()
        strategy = results[0]

        analysis = strategy.analyzers.neutral_band.get_analysis()
        assert 'neutral_count' in analysis
        assert 'neutral_rate' in analysis
        assert 'win_count' in analysis
        assert 'loss_count' in analysis
        assert 'win_rate' in analysis
        assert 'effective_total' in analysis

    def test_analyzer_custom_band(self, mock_stock_data):
        """测试自定义中性带宽"""
        from backtest.strategies import get_strategy_class
        DualMAStrategy = get_strategy_class('dual_ma_crossover')

        cerebro = bt.Cerebro()
        data = bt.feeds.PandasData(dataname=mock_stock_data)
        cerebro.adddata(data)
        cerebro.addstrategy(DualMAStrategy, fast_period=5, slow_period=20)
        cerebro.addanalyzer(NeutralBandAnalyzer, _name='neutral_band', neutral_band_pct=5.0)
        cerebro.broker.setcash(100000)
        results = cerebro.run()
        strategy = results[0]

        analysis = strategy.analyzers.neutral_band.get_analysis()
        assert analysis['neutral_band_pct'] == 5.0

    def test_analyzer_no_trades(self):
        """测试无交易时的分析器"""
        cerebro = bt.Cerebro()
        # 创建一个只有1天的数据，不会产生交易
        dates = pd.date_range('2025-01-01', periods=1, freq='B')
        df = pd.DataFrame({
            'open': [100], 'high': [101], 'low': [99], 'close': [100], 'volume': [10000],
        }, index=dates)
        data = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(data)
        cerebro.addanalyzer(NeutralBandAnalyzer, _name='neutral_band')
        cerebro.broker.setcash(100000)
        results = cerebro.run()
        strategy = results[0]

        analysis = strategy.analyzers.neutral_band.get_analysis()
        assert analysis['neutral_count'] == 0
        assert analysis['effective_total'] == 0
        assert analysis['win_rate'] == 0

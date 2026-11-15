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
        # 没有有效样本时胜率必须是 None，不是 0（2026-10-18 事故：0 会把
        # 「全部交易落在 ±2% 中性带内、无从判断」冒充成「胜率 0%」）
        assert analysis['win_rate'] is None

class TestNeutralBandRealTrades:
    """回归护栏（2026-10-18 事故）：平仓时 trade.size 已经是 0，
    旧实现用 trade.price * abs(trade.size) 作分母 → pnl_pct 恒为 0 → 所有交易判成「中性」
    → 胜率永远算不出来。把带宽从 2% 收到 0.1% 都没变化，才暴露出来。
    这里跑一笔真实买卖，锁死「中性带真的是按收益率算的」。"""

    def test_closed_trade_has_real_pnl_pct(self):
        import backtrader as bt
        import pandas as pd

        class OneTrade(bt.Strategy):
            def __init__(self):
                self.done = False

            def next(self):
                if self.done:
                    return
                if len(self) == 1:
                    self.buy(size=100)
                elif len(self) == 6:
                    self.close()
                    self.done = True

        dates = pd.date_range('2025-01-01', periods=10, freq='B')
        # 第 1 天买、第 6 天卖，收盘价 100 → 110（+10%）
        close = [100, 101, 102, 104, 106, 110, 108, 107, 106, 105]
        df = pd.DataFrame({
            'open': close, 'high': [c + 1 for c in close],
            'low': [c - 1 for c in close], 'close': close,
            'volume': [10000] * 10,
        }, index=dates)

        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=df))
        cerebro.addstrategy(OneTrade)
        cerebro.addanalyzer(NeutralBandAnalyzer, _name='nb')
        cerebro.broker.setcash(100000)
        cerebro.broker.setcommission(commission=0.0)
        strat = cerebro.run()[0]
        a = strat.analyzers.nb.get_analysis()

        assert a['neutral_count'] == 0, '一笔 +10% 的交易不该被判成中性（旧 bug 会全判中性）'
        assert a['effective_total'] == 1
        assert a['win_rate'] == 1.0

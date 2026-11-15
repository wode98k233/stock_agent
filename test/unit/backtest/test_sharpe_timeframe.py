"""夏普口径：必须按**日收益 + 年化**算，否则整条选优/过拟合红线是空转。

真实事故（2026-10-14 冒烟 6 个想法）：8 轮 × 6 个 run 的**样本外夏普全部算不出来**。
根因不是数据，而是 backtrader 的 SharpeRatio 默认 `timeframe=TimeFrame.Years`：

- 一年期 OOS 只有 1 个「年收益」样本 → 标准差无从计算 → sharperatio=None；
- 两年期 IS 只有 2 个样本 → 算出来的是「两点夏普」，统计上没有意义；
- 实测：默认配置下 200 根日线的普通买入持有也返回 None。

后果：`select_best` 的候选全是 None/0（见 test_sharpe_unknown.py），
「样本外夏普 > 0」「IS/OOS 夏普比 > 1.3」这些红线等于没生效。
"""
import math
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import SHARPE_ANALYZER_KWARGS


def _synthetic_frame(n=200):
    rows = []
    price = 100.0
    for i in range(n):
        price *= (1 + 0.004 * math.sin(i / 5.0) + (0.002 if i % 3 == 0 else -0.001))
        rows.append((price, price * 1.01, price * 0.99, price))
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'])
    df.index = pd.date_range('2025-01-01', periods=n, freq='D')
    return df


def _sharpe(**kwargs):
    import backtrader as bt

    class BuyHold(bt.Strategy):
        def next(self):
            if not self.position:
                self.buy()

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=_synthetic_frame()))
    cerebro.addstrategy(BuyHold)
    cerebro.broker.setcash(1_000_000)
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', **kwargs)
    st = cerebro.run()[0]
    return st.analyzers.sharpe.get_analysis().get('sharperatio')


def test_engine_analyzer_kwargs_are_daily_and_annualized():
    import backtrader as bt
    assert SHARPE_ANALYZER_KWARGS['timeframe'] == bt.TimeFrame.Days, '默认 Years 会退化成 1 个样本'
    assert SHARPE_ANALYZER_KWARGS['annualize'] is True
    assert SHARPE_ANALYZER_KWARGS['riskfreerate'] == 0.0, 'rf 换算在本版本会给出 -5000 级荒谬值'


def test_engine_kwargs_produce_a_number():
    """用引擎的口径跑一段有波动的日线：必须算得出来（回归守卫）"""
    value = _sharpe(**SHARPE_ANALYZER_KWARGS)
    assert isinstance(value, float) and value == value, value
    assert abs(value) < 100, '年化日夏普不该是这种量级：%s' % value


def test_default_years_timeframe_is_the_trap():
    """锁住「默认配置算不出来」这个事实：以后谁把它改回去，这条会红"""
    assert _sharpe(riskfreerate=0.03) is None

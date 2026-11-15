"""滑点接线测试（P0 设计 §6.3）。

覆盖：setup_broker 接线（slip_perc / slip_open）、slippage=0 不接线、
成交价含滑点且 broker 记账、滑点不越出 bar 边界（slip_match）、
策略侧逐笔滑点成本核算（读 bt_ideal_price）。
"""
from unittest.mock import patch

import backtrader as bt
import pandas as pd

from backtest.a_stock_broker import setup_broker
from backtest.data_adapter import AStockPandasData, _normalize_columns

_CONFIG = {
    'code': '600519', 'asset_type': 'stock', 'initial_cash': 100000.0,
    'slippage': 0.002, 'commission': 0.0003, 'min_commission': 5.0,
}


def _df(rows):
    frame = pd.DataFrame([{
        'trade_date': f'2025-01-{i + 2:02d}',
        'open': r[0], 'high': r[1], 'low': r[2], 'close': r[3],
        'volume': r[4], 'pre_close': r[5],
    } for i, r in enumerate(rows)])
    return _normalize_columns(frame)


class _BuyOnBar(bt.Strategy):
    params = (('buy_bar', 2),)

    def __init__(self):
        self.order = None

    def notify_order(self, order):
        if order.status not in (order.Submitted, order.Accepted):
            self.order = None

    def next(self):
        self.broker.set_coc(False)
        if len(self) == self.p.buy_bar and self.order is None and not self.position:
            self.order = self.buy(size=100)


def _setup_broker_only(config):
    cerebro = bt.Cerebro()
    with patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        setup_broker(cerebro, config)
    return cerebro


def _run_with_slippage(df, config):
    data = AStockPandasData(dataname=df, datetime=None, open='open', high='high',
                            low='low', close='close', volume='volume',
                            pre_close='pre_close', openinterest=-1)
    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(_BuyOnBar)
    with patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        setup_broker(cerebro, dict(config))
    strat = cerebro.run()[0]
    return cerebro.broker, strat


def test_setup_wires_slippage_perc_with_slip_open():
    cerebro = _setup_broker_only(dict(_CONFIG))
    assert cerebro.broker.p.slip_perc == 0.002
    assert cerebro.broker.p.slip_open is True, 'slip_open 必须为 True（否则开盘价不滑、等于白配）'


def test_zero_slippage_keeps_default():
    cerebro = _setup_broker_only(dict(_CONFIG, slippage=0))
    assert cerebro.broker.p.slip_perc == 0.0


def test_fill_price_includes_slippage_and_ledger():
    """买入成交价 = open × (1+slip)，且 broker.slippage_cost 逐笔记账 > 0"""
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),   # bar2 下单
        (10.0, 10.2, 9.9, 10.1, 100000, 10.0),   # bar3 open=10.0 成交（滑点后 10.02）
        (10.1, 10.2, 10.0, 10.1, 100000, 10.1),
    ])
    broker, strat = _run_with_slippage(df, _CONFIG)
    assert abs(strat.position.price - 10.0 * 1.002) < 1e-6, '成交价必须含滑点'
    assert abs(broker.slippage_cost - abs(10.0 * 1.002 - 10.0) * 100) < 1e-6


def test_slippage_not_beyond_bar_high():
    """slip_match=True：滑点价越界时夹到 bar 边界，不产生不存在的价格"""
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),
        (10.0, 10.0, 10.0, 10.0, 100000, 10.0),  # bar3 open=high=10.0：滑点价 10.02 越界
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),
    ])
    broker, strat = _run_with_slippage(df, _CONFIG)
    assert strat.position.price <= 10.0 + 1e-9, '滑点价不得越出 bar high'
    assert broker.slippage_cost == 0.0, '夹边界后无实际滑点成本'


def test_strategy_calc_slippage_cost_reads_bt_ideal_price():
    """策略侧逐笔滑点成本：读 order.info['bt_ideal_price']（P0 §4.7-2）。

    ``_calc_slippage_cost`` 不触碰实例状态，可用 ``None`` 作占位 self。
    """
    from backtest.strategy_base import StockSolveStrategy

    class _Exec:
        price, size = 10.02, 100

    class _Order:
        executed = _Exec()
        info = {'bt_ideal_price': 10.0}

    cost = StockSolveStrategy._calc_slippage_cost(None, _Order())
    assert abs(cost - 2.0) < 1e-9

    class _OrderNoIdeal:
        executed = _Exec()
        info = {}

    assert StockSolveStrategy._calc_slippage_cost(None, _OrderNoIdeal()) == 0.0

"""价格限制 Broker 端到端测试（P0 设计 §6.2）。

手工构造 DataFrame（不碰 DB），真跑迷你 Cerebro——覆盖：
涨停拒买 / 跌停拒卖 / 被拒即作废（零重试）/ 停牌 / same_close 口径（创建 bar
快照）/ off·warn 档 / 非 stock 放行。
"""
import backtrader as bt
import pandas as pd
import pytest

from backtest.a_stock_broker import AStockBroker
from backtest.data_adapter import AStockPandasData, _normalize_columns

RULE = {'code': '600519', 'limit_pct': 0.10, 'mode': 'enforce'}


def _df(rows):
    """rows: list of (open, high, low, close, volume, pre_close)"""
    frame = pd.DataFrame([{
        'trade_date': f'2025-01-{i + 2:02d}',
        'open': r[0], 'high': r[1], 'low': r[2], 'close': r[3],
        'volume': r[4], 'pre_close': r[5],
    } for i, r in enumerate(rows)])
    return _normalize_columns(frame)


class _BuyOnBar(bt.Strategy):
    """第 buy_bar 根以市价买入（coc 可选；snapshot 模拟 strategy_base 的创建 bar 快照）"""
    params = (('buy_bar', 2), ('coc', False), ('snapshot', True))

    def __init__(self):
        self.order = None

    def notify_order(self, order):
        if order.status not in (order.Submitted, order.Accepted):
            self.order = None

    def next(self):
        self.broker.set_coc(self.p.coc)
        if len(self) == self.p.buy_bar and self.order is None and not self.position:
            o = self.buy(size=100)
            self.order = o
            if self.p.snapshot:
                o.info['bt_pre_close'] = float(self.data.pre_close[0])


class _BuyThenSell(bt.Strategy):
    """第 buy_bar 根买入、第 sell_bar 根卖出（均市价、非 coc）"""
    params = (('buy_bar', 1), ('sell_bar', 3))

    def __init__(self):
        self.order = None

    def notify_order(self, order):
        if order.status not in (order.Submitted, order.Accepted):
            self.order = None

    def next(self):
        self.broker.set_coc(False)
        if len(self) == self.p.buy_bar and self.order is None and not self.position:
            self.order = self.buy(size=100)
        elif len(self) == self.p.sell_bar and self.order is None and self.position:
            self.order = self.sell(size=self.position.size)


def _run(df, strategy_cls, rule_ctx=None, cash=100000.0, **strat_kwargs):
    broker = AStockBroker(rule_ctx=dict(rule_ctx or RULE))
    data = AStockPandasData(
        dataname=df, datetime=None,
        open='open', high='high', low='low', close='close',
        volume='volume', pre_close='pre_close', openinterest=-1)
    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(strategy_cls, **strat_kwargs)
    cerebro.setbroker(broker)
    broker.setcash(cash)
    strat = cerebro.run()[0]
    return broker, strat


# ── 用例 1：涨停拒买 ──────────────────────────────────────────────

def test_limit_up_blocks_buy():
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),   # bar2 下单
        (11.0, 11.0, 11.0, 11.0, 100000, 10.0),  # bar3 一字涨停（+10%）
        (11.0, 11.1, 10.9, 11.0, 100000, 11.0),
        (11.0, 11.1, 10.9, 11.0, 100000, 11.0),
    ])
    broker, strat = _run(df, _BuyOnBar)
    assert broker.blocked['limit_up'] == 1
    assert strat.position.size == 0, '涨停日的买单不允许成交'


# ── 用例 2：跌停拒卖 ──────────────────────────────────────────────

def test_limit_down_blocks_sell():
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),   # bar2 买单成交
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),   # bar3 发卖单
        (9.0, 9.0, 9.0, 9.0, 100000, 10.0),      # bar4 一字跌停（-10%）
        (9.0, 9.1, 8.9, 9.0, 100000, 9.0),
    ])
    broker, strat = _run(df, _BuyThenSell)
    assert broker.blocked['limit_down'] == 1
    assert strat.position.size == 100, '跌停日的卖单不允许成交（持仓仍在）'


# ── 用例 3：被拒即作废（零重试） ──────────────────────────────────

def test_blocked_order_expires_next_bar_zero_retry():
    """连续 3 根一字涨停：成交尝试**仅 1 次**（limit_up==1），次根即作废。

    若实现退化为「隔日重试」，limit_up 会累计多次——本用例专门锁死零重试语义
    （P0 §3.3）。
    """
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),      # bar2 下单
        (11.0, 11.0, 11.0, 11.0, 100000, 10.0),     # bar3 涨停：被拒（唯一一次尝试）
        (12.1, 12.1, 12.1, 12.1, 100000, 11.0),     # bar4 再涨停：此时订单已作废
        (13.31, 13.31, 13.31, 13.31, 100000, 12.1),  # bar5
    ])
    broker, strat = _run(df, _BuyOnBar)
    assert broker.blocked['limit_up'] == 1, '被拒订单不得隔日重试（零重试语义）'
    assert broker.blocked['expired'] == 1, '被拒订单应在次根被 cancel（当日有效）'
    assert strat.position.size == 0, '全程 0 成交'


# ── 用例 4：停牌（volume=0 占位行） ───────────────────────────────

def test_suspended_volume_zero():
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),   # bar2 下单
        (10.0, 10.0, 10.0, 10.0, 0, 10.0),       # bar3 停牌占位行
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),
    ])
    broker, strat = _run(df, _BuyOnBar)
    assert broker.blocked['suspended'] == 1
    assert broker.blocked['expired'] == 1
    assert strat.position.size == 0


# ── 用例 5：same_close 口径用创建 bar 的 pre_close ────────────────

def test_same_close_uses_created_bar_pre_close():
    """coc 成交价 = 创建当根收盘（11.0），判定必须用**创建根** pre_close（10.0）→ 拦。

    若误用撮合当根（bar3）的 pre_close（11.0），chg=0 会放行——本用例防这个回归。
    """
    df = _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.9),
        (11.0, 11.0, 10.5, 11.0, 100000, 10.0),  # bar2 收盘涨停（+10%），coc 单创建于此
        (11.1, 11.2, 11.0, 11.1, 100000, 11.0),  # bar3：若错用此根 pre_close 会放行
        (11.1, 11.2, 11.0, 11.1, 100000, 11.1),
    ])
    broker, strat = _run(df, _BuyOnBar, coc=True)
    assert broker.blocked['limit_up'] == 1, 'same_close 必须用创建 bar 的 pre_close 判定'
    assert strat.position.size == 0


# ── 用例 6：off / warn 档 ────────────────────────────────────────

def _limit_up_df():
    return _df([
        (10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        (10.0, 10.1, 9.9, 10.0, 100000, 10.0),   # bar2 下单
        (11.0, 11.0, 11.0, 11.0, 100000, 10.0),  # bar3 一字涨停
        (11.0, 11.1, 10.9, 11.0, 100000, 11.0),
        (11.0, 11.1, 10.9, 11.0, 100000, 11.0),
    ])


def test_off_mode_no_block_no_record():
    broker, strat = _run(_limit_up_df(), _BuyOnBar, rule_ctx={**RULE, 'mode': 'off'})
    assert broker.blocked['limit_up'] == 0 and broker.blocked['expired'] == 0
    assert strat.position.size == 100, 'off 档：涨停日照样成交（与改动前逐位一致）'


def test_warn_mode_records_but_passes():
    broker, strat = _run(_limit_up_df(), _BuyOnBar, rule_ctx={**RULE, 'mode': 'warn'})
    assert broker.blocked['limit_up'] == 1, 'warn 档要记录归因'
    assert strat.position.size == 100, 'warn 档不拦——成交价与旧行为一致'
    assert abs(strat.position.price - 11.0) < 1e-6


# ── 用例 7：非 stock 放行（limit_pct=None） ──────────────────────

def test_non_stock_no_limit():
    broker, strat = _run(
        _limit_up_df(), _BuyOnBar,
        rule_ctx={'code': '159915', 'limit_pct': None, 'mode': 'enforce'})
    assert broker.blocked['limit_up'] == 0
    assert strat.position.size == 100, '无涨跌停约束（ETF）：一字板照样成交'

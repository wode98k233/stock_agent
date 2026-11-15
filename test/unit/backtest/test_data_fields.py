"""数据字段接线与 fail-closed 契约（2026-10-20 排查后的回归）。

核心契约：
1. ``DATA_REF_MAP`` 里声明的字段必须在 feed 里真实存在；
2. ``pct_change`` 取的是当日涨跌幅，**不是**收盘价；
3. 未声明/未实现字段（如 turnover_rate）必须编译期记 unsupported、运行期恒假，
   绝不能再静默回退成 close；
4. ETF/部分源缺失的 pct_change 用 close/pre_close 推算，单位仍是百分数。
"""
import json
import sys
from pathlib import Path

import backtrader as bt
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.a_stock_broker import setup_broker  # noqa: E402
from backtest.data_adapter import (AStockPandasData, _fill_derived_fields,  # noqa: E402
                                   _fill_pre_close)
from backtest.json_compiler import compile_strategy, validate_strategy_json  # noqa: E402


def _spec(rules):
    return {
        'meta': {'strategy_id': 'data_field_probe', 'name': 'data_field_probe'},
        'params': {},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': rules},
            'sell': {'logic': 'AND', 'rules': [], 'hold_bars': 1},
        },
    }


def _df(rows):
    df = pd.DataFrame(rows, columns=[
        'trade_date', 'open', 'high', 'low', 'close',
        'pre_close', 'pct_change', 'amount', 'change_amount'])
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    return df.set_index('trade_date')


def _run(df, rules):
    cls = compile_strategy(json.dumps(_spec(rules), ensure_ascii=False))
    cerebro = bt.Cerebro()
    cerebro.adddata(AStockPandasData(dataname=df))
    cerebro.addstrategy(cls, entry_timing='same_close', exit_timing='next_open')
    setup_broker(cerebro, {'code': '600519', 'asset_type': 'stock',
                           'initial_cash': 100000, 'slippage': 0,
                           'price_limit_mode': 'off'})
    st = cerebro.run()[0]
    return [t for t in st.get_trades() if t['direction'] == 'buy'], cls


# 收盘价都在 10 元上方，但日涨幅都 <1%；只有最后一根 +6%
ROWS = [
    ('2024-01-02', 10.00, 10.05, 9.95, 10.00, 10.00, 0.0, 1e8, 0.0),
    ('2024-01-03', 10.00, 10.06, 9.96, 10.04, 10.00, 0.4, 1e8, 0.04),
    ('2024-01-04', 10.04, 10.10, 10.00, 10.06, 10.04, 0.2, 1e8, 0.02),
    ('2024-01-05', 10.06, 10.70, 10.05, 10.66, 10.06, 6.0, 3e8, 0.60),
    ('2024-01-08', 10.70, 10.80, 10.65, 10.75, 10.66, 0.84, 1e8, 0.09),
]


def test_feed_exposes_declared_data_lines():
    """DATA_REF_MAP 的字段必须真的挂在 feed 上——否则就是幽灵字段。"""
    data = AStockPandasData(dataname=_df(ROWS))
    aliases = set(data.lines.getlinealiases())
    for name in ('pre_close', 'pct_change', 'amount', 'change_amount'):
        assert name in aliases, f'{name} 没接到 feed：{aliases}'


def test_pct_change_uses_gain_not_close():
    """pct_change > 5 只命中 +6% 那根，不能变成 close > 5（否则几乎每天都买）。"""
    buys, _cls = _run(_df(ROWS), [
        {'type': 'indicator', 'op': '>', 'left': {'data': 'pct_change'}, 'right': {'value': 5}},
    ])
    assert len(buys) == 1, buys
    assert buys[0]['trade_date'] == '2024-01-05', buys

    # 同一份行情，close > 5 会命中每一根——用来证明两者不再等价
    buys_close, _cls = _run(_df(ROWS), [
        {'type': 'indicator', 'op': '>', 'left': {'data': 'close'}, 'right': {'value': 5}},
    ])
    assert len(buys_close) > 1, buys_close


def test_amount_uses_real_amount():
    buys, _cls = _run(_df(ROWS), [
        {'type': 'indicator', 'op': '>', 'left': {'data': 'amount'}, 'right': {'value': 2e8}},
    ])
    assert len(buys) == 1 and buys[0]['trade_date'] == '2024-01-05', buys


def test_unimplemented_field_fails_closed():
    """turnover_rate 已从能力表删除：编译记 unsupported，运行期 0 成交。"""
    rules = [{'type': 'indicator', 'op': '>', 'left': {'data': 'turnover_rate'},
              'right': {'value': 3}}]
    buys, cls = _run(_df(ROWS), rules)
    assert buys == []
    assert getattr(cls, '_unsupported_conditions', []), '缺失字段必须留下归因'
    assert any(g.get('name') == 'turnover_rate'
               for g in getattr(cls, '_capability_gaps', []))


def test_validate_rejects_unimplemented_field():
    errors = validate_strategy_json(json.dumps(_spec([
        {'type': 'indicator', 'op': '>', 'left': {'data': 'amplitude'}, 'right': {'value': 3}},
    ]), ensure_ascii=False))
    assert errors and 'amplitude' in errors[0]


def test_derived_pct_change_from_close_pre_close():
    """ETF/部分源 pct_change 为空时，用 close/pre_close 推算（百分数）。"""
    rows = [
        ('2024-01-02', 1.00, 1.01, 0.99, 1.00, None, None, None, None),
        ('2024-01-03', 1.00, 1.06, 0.99, 1.05, None, None, None, None),
        ('2024-01-04', 1.05, 1.07, 1.04, 1.06, None, None, None, None),
    ]
    df = pd.DataFrame(rows, columns=['trade_date', 'open', 'high', 'low', 'close',
                                     'pre_close', 'pct_change', 'amount', 'change_amount'])
    df, _ = _fill_pre_close(df)
    df = _fill_derived_fields(df)
    assert df['pct_change'].iloc[1] == pytest.approx(5.0)
    assert df['pct_change'].iloc[2] == pytest.approx((1.06 / 1.05 - 1) * 100)
    assert df['change_amount'].iloc[1] == pytest.approx(0.05)

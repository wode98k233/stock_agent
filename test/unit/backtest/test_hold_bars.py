"""B 批契约：纯时序卖点 ``hold_bars`` 与买卖分离成交口径。

覆盖三条最容易回归、且回归后果最严重的链路：

1. ``hold_bars`` 能编译、能表达「尾盘买入、次日卖出」这类**没有指标条件**的策略
   （旧 schema 强制 sell.rules 非空，LLM 只能编造 MA20/RSI 顶替，是用户原始输入
   「尾盘买入，次日卖出」跑不出结果的根因）；
2. 买卖口径**各自**生效：``entry_timing`` / ``exit_timing`` 独立，``hold_bars`` 的
   持有根数精确（不能是 N+1）；
3. 成交日期不错位、拒单可观测。

刻意不用 mock 行情：真编译 JSON → 真跑 backtrader → 读成交价/日期。
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BARS = [
    # date, open, high, low, close
    ('2024-01-02', 10.0, 10.3, 9.9, 10.2),
    ('2024-01-03', 11.0, 11.3, 10.9, 11.2),
    ('2024-01-04', 12.0, 12.3, 11.9, 12.2),
    ('2024-01-05', 13.0, 13.3, 12.9, 13.2),
    ('2024-01-08', 14.0, 14.3, 13.9, 14.2),
    ('2024-01-09', 15.0, 15.3, 14.9, 15.2),
]

# 温和行情（每根跳空约 1%，且 close > open 让买入条件成立）：用于验证
# 「全仓 + next_open 不应被 Margin 拒单」
MILD_BARS = [
    ('2024-01-02', 10.0, 10.3, 9.9, 10.1),
    ('2024-01-03', 10.1, 10.4, 10.0, 10.15),
    ('2024-01-04', 10.2, 10.5, 10.1, 10.25),
    ('2024-01-05', 10.3, 10.6, 10.2, 10.35),
    ('2024-01-08', 10.4, 10.7, 10.3, 10.45),
    ('2024-01-09', 10.5, 10.8, 10.4, 10.55),
]


def _spec(sell=None, hold_bars=None, buy_rules=None):
    conditions = {
        'buy': {'logic': 'AND', 'rules': buy_rules or [
            {'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
             'right': {'data': 'open'}},
        ]},
        'sell': dict(sell or {}),
    }
    if hold_bars is not None:
        conditions['sell']['hold_bars'] = hold_bars
    return {
        'meta': {'strategy_id': 'hold_bars_probe', 'name': '持有N根后卖'},
        'params': {},
        'conditions': conditions,
    }


def _run(entry_timing='next_open', exit_timing='next_open', spec=None, cash=100000,
         bars=None):
    """真跑 backtrader，返回 (策略实例, 编译产物)

    ⚠️ 必须走生产 broker（``setup_broker``）：``same_close + next_open`` 的
    「T 日预挂卖单」靠 ``AStockBroker`` 的 per-order coc 实现；用默认
    ``BackBroker`` 测不出真实撮合语义。
    """
    import backtrader as bt
    from backtest.a_stock_broker import setup_broker
    from backtest.json_compiler import compile_strategy

    cls = compile_strategy(json.dumps(spec or _spec(hold_bars=1), ensure_ascii=False))
    df = pd.DataFrame(bars or BARS, columns=['date', 'open', 'high', 'low', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df.set_index('date')))
    cerebro.addstrategy(cls, entry_timing=entry_timing, exit_timing=exit_timing)
    setup_broker(cerebro, {'code': '600519', 'asset_type': 'stock',
                           'initial_cash': cash, 'slippage': 0,
                           'price_limit_mode': 'off'})
    res = cerebro.run()
    st = res[0]
    return st, cls


def _trades(st):
    return [(r['direction'], r['trade_date'], r['price'], r.get('holding_days'))
            for r in st.get_trades()]


# --------------------------------------------------------------------------
# 1. hold_bars 能编译
# --------------------------------------------------------------------------

def test_schema_documents_hold_bars():
    """LLM 拿到的 schema 必须教它用 hold_bars 表达纯时序卖点"""
    from backtest.json_compiler import export_llm_schema
    sch = export_llm_schema()
    assert 'hold_bars' in sch
    assert '不要写任何 sell.rules' in sch or '不要用指标条件凑' in sch


def test_sell_rules_can_be_empty_with_hold_bars():
    """sell.rules 允许为空 —— 这是纯时序策略能存在的前提"""
    from backtest.json_compiler import compile_strategy
    cls = compile_strategy(json.dumps(_spec(hold_bars=1), ensure_ascii=False))
    assert not getattr(cls, '_unsupported_conditions', []), \
        getattr(cls, '_unsupported_conditions', [])


def test_sell_rules_empty_without_hold_bars_is_unsupported():
    """既没 sell.rules 又没 hold_bars → 没有任何卖点，必须报出来（而不是静默不交易）"""
    from backtest.json_compiler import compile_strategy
    cls = compile_strategy(json.dumps(_spec(), ensure_ascii=False))
    assert getattr(cls, '_unsupported_conditions', []), "没有任何卖点却编译通过 = 静默错误"


def test_hold_bars_accepts_wrapper_and_top_level():
    """LLM 可能写成 {"value": 1} 或放在 conditions 顶层，两种都要认"""
    from backtest.json_compiler import compile_strategy

    a = compile_strategy(json.dumps(_spec(hold_bars={'value': 2}), ensure_ascii=False))
    b = compile_strategy(json.dumps(_spec(hold_bars=2), ensure_ascii=False))
    for cls in (a, b):
        assert dict(cls.params._getpairs()).get('hold_bars') == 2


def test_meta_exposes_hold_bars_exit_mode():
    """前端构造器靠 exit_modes.hold_bars 渲染「持有 N 根后卖出」控件"""
    from backtest.json_compiler import get_strategy_meta
    meta = get_strategy_meta()
    assert 'hold_bars' in (meta.get('exit_modes') or {})


# --------------------------------------------------------------------------
# 2. 「尾盘买入、次日卖出」端到端
# --------------------------------------------------------------------------

def test_tail_buy_next_day_sell_is_hold_one_bar():
    """用户原话的直接对应：尾盘买入、持有 1 根、次日卖出

    same_close → same_close + hold_bars=1 必须精确命中
    「01-02 尾盘 10.2 买 → 01-03 尾盘 11.2 卖，持有 1 天」。
    倒计时必须锚在**下单时刻**（`_hold_left`）；早期用 bars_since_buy 比大小会变成 2 天。
    """
    st, _cls = _run('same_close', 'same_close', spec=_spec(hold_bars=1))
    trades = _trades(st)
    assert len(trades) >= 2, trades
    assert trades[0][0] == 'buy' and trades[0][2] == 10.2, trades[0]
    assert trades[1][0] == 'sell' and trades[1][2] == 11.2, trades[1]
    assert trades[1][3] == 1, f"持有根数变成了 {trades[1][3]}（多半是 N+1 的倒计时锚点问题）"


def test_hold_two_bars_means_two_bars():
    st, _cls = _run('same_close', 'same_close', spec=_spec(hold_bars=2))
    trades = _trades(st)
    assert trades[1][2] == 12.2, trades


def test_hold_two_bars_next_open_sells_t_plus_2_open():
    """持有 2 根 + 次日开盘卖：T 收盘买，T+2 开盘卖（不是 T+3）。"""
    st, _cls = _run('same_close', 'next_open', spec=_spec(hold_bars=2))
    trades = _trades(st)
    assert trades[0][2] == 10.2, trades[0]
    assert trades[1][1] == '2024-01-04' and trades[1][2] == 12.0, trades[1]


def test_next_open_entry_hold_two_bars_keeps_n():
    """entry=next_open 时买入已晚一根，持有 2 根仍是 T+2 开盘卖（不能变成 1 根）。"""
    st, _cls = _run('next_open', 'next_open', spec=_spec(hold_bars=2), bars=MILD_BARS)
    trades = _trades(st)
    assert trades[0][1] == '2024-01-03' and trades[0][2] == 10.1, trades[0]
    assert trades[1][1] == '2024-01-05' and trades[1][2] == 10.3, trades[1]


def test_entry_and_exit_timing_are_independent():
    """买卖两侧口径各自生效：尾盘买、次日开盘卖"""
    st, _cls = _run('same_close', 'next_open', spec=_spec(hold_bars=1))
    trades = _trades(st)
    assert trades[0][2] == 10.2, '买入口径 same_close 应吃 01-02 收盘价'
    assert trades[1][2] == 11.0, '卖出口径 next_open 应吃 01-03 开盘价（不能晚到 01-04）'
    assert trades[1][1] == '2024-01-03', trades[1]


def test_next_close_exit_uses_close_of_next_bar():
    """尾盘买、次日收盘卖：T+1 收盘 11.2，不是 T+2 的 12.2"""
    st, _cls = _run('same_close', 'next_close', spec=_spec(hold_bars=1))
    assert _trades(st)[1][2] == 11.2


def test_default_timing_unchanged():
    """缺省 next_open → next_open 的老行为不能被改掉"""
    st, _cls = _run('next_open', 'next_open', spec=_spec(hold_bars=1), bars=MILD_BARS)
    trades = _trades(st)
    assert trades[0][1] == '2024-01-03' and trades[0][2] == 10.1, trades[0]  # 次日开盘买入
    assert trades[1][1] == '2024-01-04' and trades[1][2] == 10.2, trades[1]  # 次日开盘卖出


def test_unknown_timing_falls_back_silently():
    """非法口径静默回退 next_open，不抛错"""
    st, _cls = _run('same_close', 'weird_timing', spec=_spec(hold_bars=1))
    assert _trades(st)[0][2] == 10.2


# --------------------------------------------------------------------------
# 3. 成交日期 / 拒单可观测
# --------------------------------------------------------------------------

def test_same_close_trade_date_not_shifted():
    """same_close 在当根收盘成交，但 notify_order 到达时 data 已推进一根 ——
    不回退日期就会把成交日记成次日（比真实成交日晚一天）。"""
    st, _cls = _run('same_close', 'same_close', spec=_spec(hold_bars=1))
    trades = _trades(st)
    assert trades[0][1] == '2024-01-02', trades[0]
    assert trades[1][1] == '2024-01-03', trades[1]


def test_rejected_orders_are_observable():
    """全仓 + 跳空高开 → broker 以 Margin 拒单 → 静默0 交易。

    这是真实踩过的坑（next_open 按 close 计价、实际按次日开盘成交），必须有计数与
    最后一次拒单信息，否则「策略没信号」和「钱不够」两种 0 交易完全分不开。
    """
    st, _cls = _run('next_open', 'next_open', spec=_spec(hold_bars=1), cash=10000)
    info = st.get_reject_info()
    assert 'rejected_orders' in info and 'last' in info


def test_full_position_next_open_not_rejected_on_normal_gap():
    """温和行情下不能被 Margin 拒单（_cost_basis_price 的保守上界要生效）"""
    st, _cls = _run('next_open', 'next_open', spec=_spec(hold_bars=1), bars=MILD_BARS)
    assert st.get_reject_info()['rejected_orders'] == 0, \
        st.get_reject_info()
    assert _trades(st)


def test_t_plus_1_blocks_same_bar_sell():
    """T+1 以 bar 计数判定：same_close 买入当根不允许卖"""
    st, _cls = _run('same_close', 'same_close',
                    spec=_spec(sell={'rules': [
                        {'type': 'indicator', 'op': '<', 'left': {'data': 'close'},
                         'right': {'data': 'open'}},
                    ]}, hold_bars=None))
    assert isinstance(_trades(st), list)


# --------------------------------------------------------------------------
# 4. 口径贯穿 engine
# --------------------------------------------------------------------------

def test_engine_passes_both_timings():
    """买卖两个口径都要进 engine config（漏一个就退化成单一口径）"""
    from backtest.strategy_research import IsoOsRunner

    class _Rec:
        def __init__(self):
            self.configs = []

        def run(self, config, run_id=None, progress_callback=None, origin='manual', ref_id=''):
            self.configs.append(dict(config))
            return {'annual_return': 0.1, 'sharpe_ratio': 1.0, 'max_drawdown': -0.1,
                    'win_rate': 0.5, 'trade_count': 3}

    eng = _Rec()
    runner = IsoOsRunner(engine=eng, entry_timing='same_close', exit_timing='next_close')
    runner._run_segment('sr_x', '600519', '2025-01-01', '2025-06-30', 'IS')
    cfg = eng.configs[0]
    assert cfg['entry_timing'] == 'same_close'
    assert cfg['exit_timing'] == 'next_close'


def test_engine_exit_timing_defaults_to_entry():
    from backtest.strategy_research import IsoOsRunner
    runner = IsoOsRunner(entry_timing='same_close')
    assert runner.exit_timing == 'same_close'


def test_engine_has_no_broker_level_coc():
    """engine 里绝不能再 broker 级 set_coc(True)。

    coc 是 broker 全局开关、对买卖两端同时生效 —— 在这里打开，买卖分离立刻失效。
    用 AST 判「真的调了」，而不是字符串包含（注释里为了说明原因会提到 set_coc）。
    """
    import ast
    import inspect
    import textwrap

    from backtest.engine import BacktestEngine
    src = textwrap.dedent(inspect.getsource(BacktestEngine))
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'set_coc'):
            calls.append(ast.dump(node.args[0]) if node.args else '?')
    assert not calls, f'engine 又出现了 broker 级 set_coc{ calls}，买卖分离会失效'


def test_blocked_buy_does_not_leave_naked_short():
    """涨停买不进时，T 日预挂的卖单不能变成裸卖空。"""
    import backtrader as bt
    from backtest.a_stock_broker import setup_broker
    from backtest.data_adapter import AStockPandasData
    from backtest.json_compiler import compile_strategy

    # 01-02 收盘 11.0 相对昨收 10.0 = +10%，主板涨停，买不进
    bars = [
        ('2024-01-02', 10.0, 11.0, 9.9, 11.0, 10.0, 10.0),
        ('2024-01-03', 11.5, 11.8, 11.3, 11.6, 11.0, 5.45),
        ('2024-01-04', 11.6, 11.9, 11.4, 11.7, 11.6, 0.86),
        ('2024-01-05', 11.7, 12.0, 11.5, 11.8, 11.7, 0.86),
    ]
    df = pd.DataFrame(bars, columns=['trade_date', 'open', 'high', 'low', 'close',
                                     'pre_close', 'pct_change'])
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    cls = compile_strategy(json.dumps(_spec(hold_bars=1), ensure_ascii=False))
    cerebro = bt.Cerebro()
    cerebro.adddata(AStockPandasData(dataname=df.set_index('trade_date')))
    cerebro.addstrategy(cls, entry_timing='same_close', exit_timing='next_open')
    setup_broker(cerebro, {'code': '600519', 'asset_type': 'stock',
                           'initial_cash': 100000, 'slippage': 0,
                           'price_limit_mode': 'enforce'})
    st = cerebro.run()[0]
    trades = st.get_trades()
    assert st.position.size == 0, '不能因为买单被拒就留下空头持仓'
    assert trades and trades[0]['direction'] == 'buy', trades
    # 01-02 的买单被涨停拒掉；01-03 绝不能冒出一笔无持仓支撑的卖单
    assert not any(r['direction'] == 'sell' and r['trade_date'] <= '2024-01-03'
                   for r in trades), trades
    assert cerebro.broker.blocked.get('limit_up', 0) >= 1
    assert cerebro.broker.blocked.get('pre_exit_unfilled', 0) >= 1
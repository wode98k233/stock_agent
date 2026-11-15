"""P2 契约：数据引用 `shift` 的语义 + 「回看越过数据起点」的兜底。

用户实际失败（2026-10-13，strategy_iter id=28 / run SR-20261013-1E064C）：
卖点写成 `{"data":"open"} >= {"data":"close","shift":1}`（今日开盘 ≥ 昨收），
研发首轮直接 error，失败文案退化成「原因：failed」。

根因不是「指标预热根数不足」（短窗口越界是另一回事），而是两处：

1. `shift` 被实现成 `line[shift]` —— backtrader 的 `line[i]` 取的是第 i 个**未来**
   bar（`line[-1]` 才是前一根），于是 shift=1 变成「看明天」：既造成前视偏差，
   又在最后一根 bar 上抛 `IndexError: array index out of range`；
2. 回看越过数据起点（第一根 bar 上取昨收）没有兜底，异常冒进 backtrader
   把整轮打成 error。

修法（backtest/json_compiler.py）：
- shift 语义纠正为「N 根之前」（`line[-shift]`）；负数/非整数进 `unsupported` 清单；
- 取不到历史值时返回 `_UNAVAILABLE` 哨兵，该叶子按「条件此刻不成立」处理（判假不抛）；
- 编译期统计最大回看，用 `_LookbackGuard` 把预热根数抬到 shift+1，
  让 backtrader 从够用的 bar 开始跑，而不是靠运行时兜底。

⚠️ `_LookbackGuard.addminperiod(N)` 的语义是「需要 N 根」（LineSingle 内部是
`_minperiod += N - 1`，起点 1）——写成 `addminperiod(bars - 1)` 会少一根，白改。
"""
import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 严格递增的收盘价：用来区分「看昨天」（每天成立）与「看明天」（永不成立）
BARS = [
    ('2024-01-02', 10.0, 10.3, 9.9, 10.2),
    ('2024-01-03', 11.0, 11.3, 10.9, 11.2),
    ('2024-01-04', 12.0, 12.3, 11.9, 12.2),
    ('2024-01-05', 13.0, 13.3, 12.9, 13.2),
    ('2024-01-08', 14.0, 14.3, 13.9, 14.2),
    ('2024-01-09', 15.0, 15.3, 14.9, 15.2),
]
# 温和行情（每根跳空约 1%）：next_open 全仓下单时不会被 broker 以 Margin 拒单。
# ⚠️ 跳空大 + next_open 会静默 0 交易（见 test_hold_bars.py::test_rejected_orders_are_observable），
# 验成交的用例必须配这套数据，否则「没信号」和「钱不够」分不开。
MILD_BARS = [
    ('2024-01-02', 10.0, 10.3, 9.9, 10.1),
    ('2024-01-03', 10.1, 10.4, 10.0, 10.15),
    ('2024-01-04', 10.2, 10.5, 10.1, 10.25),
    ('2024-01-05', 10.3, 10.6, 10.2, 10.35),
    ('2024-01-08', 10.4, 10.7, 10.3, 10.45),
    ('2024-01-09', 10.5, 10.8, 10.4, 10.55),
]


def _spec(buy_rules, sell_rules=None, hold_bars=None, strategy_id='shift_probe'):
    conditions = {
        'buy': {'logic': 'AND', 'rules': buy_rules},
        'sell': {'logic': 'AND', 'rules': sell_rules or []},
    }
    if hold_bars is not None:
        conditions['sell']['hold_bars'] = hold_bars
    return {
        'meta': {'strategy_id': strategy_id, 'name': 'shift 语义探针'},
        'params': {},
        'conditions': conditions,
    }


def _run(spec, bars=None):
    """真跑 backtrader（不用 mock 行情）：编译 JSON → 跑 → 返回策略实例"""
    import backtrader as bt
    from backtest.json_compiler import compile_strategy

    cls = compile_strategy(json.dumps(spec, ensure_ascii=False))
    df = pd.DataFrame(bars or BARS, columns=['date', 'open', 'high', 'low', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df.set_index('date')))
    cerebro.addstrategy(cls, entry_timing='next_open', exit_timing='next_open')
    cerebro.broker.setcash(100000)
    cerebro.broker.set_coc(False)
    res = cerebro.run()
    return res[0], cls


# --------------------------------------------------------------------------
# 1. shift 语义：N 根之前（不是未来）
# --------------------------------------------------------------------------

def test_shift_means_past_not_future():
    """`close > close[shift=1]` 在严格递增序列上必须天天成立。

    旧实现（`line[shift]` = 看明天）在这里恒假 → 0 笔交易 + 最后一根 IndexError。
    """
    spec = _spec(
        buy_rules=[{'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                    'right': {'data': 'close', 'shift': 1}}],
        hold_bars=1,
    )
    st, _cls = _run(spec, bars=MILD_BARS)
    trades = st.get_trades()
    assert trades, '看昨天 → 递增序列上必然有信号；0 笔说明又变回「看明天」了'
    first = trades[0]
    assert first['direction'] == 'buy'
    # 第一根 bar（2024-01-02）取不到「昨收」，最早只能在第二根 bar（01-03，收 10.15）
    # 出信号；next_open 成交 → 01-04 开盘价 10.2
    assert first['trade_date'] == '2024-01-04', first
    assert first['price'] == pytest.approx(10.2)


def test_shift_raises_warmup_bars():
    """回看必须并进预热根数：shift=1 → 至少 2 根才评估（否则第一根就取不到值）"""
    spec = _spec(
        buy_rules=[{'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                    'right': {'data': 'close', 'shift': 1}}],
        hold_bars=1,
    )
    st, _cls = _run(spec)
    assert st._minperiod == 2, st._minperiod


def test_large_shift_raises_warmup_and_does_not_crash():
    """shift=10 且只有 6 根数据：next() 根本不该被调用（预热不够），更不能抛"""
    spec = _spec(
        buy_rules=[{'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                    'right': {'data': 'close', 'shift': 10}}],
        hold_bars=1,
    )
    st, _cls = _run(spec)
    assert st._minperiod == 11, st._minperiod
    assert st.get_trades() == [], '数据不足时应当一根都不评估'


# --------------------------------------------------------------------------
# 2. 用户的失败用例：不再打成整轮 error
# --------------------------------------------------------------------------

def test_user_failing_rule_completes_with_trades():
    """原始失败用例：卖点 = 今日开盘 ≥ 昨收（open >= close[shift=1]）。

    修复前：`array index out of range` → run status=failed；
    修复后：正常跑完并产生交易。
    """
    spec = _spec(
        buy_rules=[{'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                    'right': {'value': 0}}],
        sell_rules=[{'type': 'indicator', 'op': '>=', 'left': {'data': 'open'},
                     'right': {'data': 'close', 'shift': 1}}],
    )
    st, _cls = _run(spec, bars=MILD_BARS)   # 修复前这里直接 IndexError
    trades = st.get_trades()
    assert trades, '买点恒真 + 卖点在递增序列上成立 → 必须有成交'
    assert st._minperiod >= 2


def test_no_index_error_when_data_shorter_than_warmup():
    """数据比预热还短：哨兵兜底（判假）而不是 IndexError"""
    spec = _spec(
        buy_rules=[{'type': 'indicator', 'op': '>=', 'left': {'data': 'open'},
                    'right': {'data': 'close', 'shift': 1}}],
        hold_bars=1,
    )
    two_bars = BARS[:2]
    st, _cls = _run(spec, bars=two_bars)
    assert st.get_trades() == []


# --------------------------------------------------------------------------
# 3. shift 取值与哨兵（不依赖 backtrader 运行）
# --------------------------------------------------------------------------

class _LineStub:
    """最小 line 替身：idx 与 __getitem__ 语义与真实 backtrader line 一致"""

    def __init__(self, values, idx):
        self.array = list(values)
        self.idx = idx

    def __getitem__(self, ago):
        return self.array[self.idx + ago]


class _StratStub:
    def __init__(self, **lines):
        self.data = types.SimpleNamespace(**lines)
        self._literals = {}
        self.p = types.SimpleNamespace()


def test_resolve_ref_reads_history_and_guards_start_of_data():
    from backtest.json_compiler import _UNAVAILABLE, _resolve_ref

    # idx=1 取「前一根」= array[0]
    assert _resolve_ref('data_close_shift_1', _StratStub(close=_LineStub([10.0, 11.0], 1))) == 10.0
    # idx=0 取「前一根」：没有历史值 → 哨兵。
    # ⚠️ 必须先判 idx 边界：直接 line[-1] 会回绕到数组末尾（Python 负索引），
    # 悄悄读到未来数据（前视偏差，比崩溃更难发现）。
    assert _resolve_ref('data_close_shift_1', _StratStub(close=_LineStub([10.0, 11.0], 0))) is _UNAVAILABLE
    # shift<=0 = 当前 bar
    assert _resolve_ref('data_close', _StratStub(close=_LineStub([10.0, 11.0], 1))) == 11.0


def test_leaf_with_unavailable_ref_is_false():
    from backtest.json_compiler import RuleNode, _eval_leaf

    node = RuleNode()
    node.logic = None                      # is_leaf 是 property：logic is None
    node.op = '>='
    node.left_ref = 'data_open'
    node.right_ref = 'data_close_shift_1'
    st = _StratStub(open=_LineStub([10.0, 11.0], 0), close=_LineStub([10.0, 11.0], 0))
    assert _eval_leaf(node, st) is False, '取不到历史值 → 条件此刻不成立（判假，不抛）'


def test_negative_shift_is_reported_not_silently_forward_looking():
    """shift 负数 = 前视偏差，必须进 unsupported 清单（按当前 bar 处理）"""
    spec = _spec(
        buy_rules=[{'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                    'right': {'data': 'close', 'shift': -1}}],
        hold_bars=1,
    )
    from backtest.json_compiler import compile_strategy

    cls = compile_strategy(json.dumps(spec, ensure_ascii=False))
    msgs = getattr(cls, '_unsupported_conditions', [])
    assert any('不支持负数' in m for m in msgs), msgs


def test_schema_tells_llm_shift_semantics():
    """给 LLM 的 schema 必须写清 shift = N 根之前（否则模型继续写未来值）"""
    from backtest.json_compiler import export_llm_schema

    schema = export_llm_schema()
    assert 'shift' in schema and 'N 根之前' in schema
    assert '不支持负数' in schema


"""操作数形态纠偏：{"value": {...}} 嵌套与未声明参数引用。

真实事故（SR-20261012-1EAEF9 第 8 轮）：LLM 把参数引用写成
`"right": {"value": {"param": "rsi_floor"}}` —— 早期实现把内层 dict 当字面量存下来，
运行到比较时才炸：

    TypeError: '>' not supported between instances of 'float' and 'dict'

异常冒到引擎层后，**整轮回测**被记成「回测执行失败」，这一轮迭代白丢。
本文件锁死两条纠偏：① 嵌套形态按内层解包（并记 unsupported，不静默）
② 引用未声明参数按恒假处理（不崩）。
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
    ('2024-01-02', 10.0, 10.3, 9.9, 10.1),
    ('2024-01-03', 10.1, 10.4, 10.0, 10.15),
    ('2024-01-04', 10.2, 10.5, 10.1, 10.25),
    ('2024-01-05', 10.3, 10.6, 10.2, 10.35),
    ('2024-01-08', 10.4, 10.7, 10.3, 10.45),
    ('2024-01-09', 10.5, 10.8, 10.4, 10.55),
]


def _spec(right, params=None, op='>'):
    return {
        'meta': {'strategy_id': 'operand_probe', 'name': '操作数形态探针'},
        'params': params if params is not None else {'floor': {'value': 10, 'min': 0, 'max': 100}},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'op': op, 'left': {'data': 'close'}, 'right': right}]},
            'sell': {'logic': 'AND', 'rules': [], 'hold_bars': 1},
        },
    }


def _run(spec):
    """真跑 backtrader：纠偏生效则能正常出成交，不生效就抛 TypeError"""
    import backtrader as bt
    from backtest.json_compiler import compile_strategy

    cls = compile_strategy(json.dumps(spec, ensure_ascii=False))
    df = pd.DataFrame(BARS, columns=['date', 'open', 'high', 'low', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df.set_index('date')))
    cerebro.addstrategy(cls, entry_timing='same_close', exit_timing='same_close')
    cerebro.broker.setcash(100000)
    cerebro.broker.set_coc(False)
    st = cerebro.run()[0]
    return st, cls


def test_param_wrapped_in_value_is_unwrapped():
    """{"value": {"param": "floor"}} → 参数引用（而不是 dict 字面量）"""
    spec = _spec({'value': {'param': 'floor'}})
    st, cls = _run(spec)
    notes = getattr(cls, '_unsupported_conditions', [])
    assert any('嵌套形态' in n for n in notes), notes
    # 收盘价 > 10（参数 floor=10）在递增序列上成立 → 必须真有成交
    assert st.get_trades(), '参数被当字面量时这里会 TypeError，0 笔说明没纠偏'


def test_double_wrapped_literal_is_unwrapped():
    """{"value": {"value": 5}} → 字面量 5"""
    spec = _spec({'value': {'value': 5}})
    st, cls = _run(spec)
    assert any('嵌套形态' in n for n in getattr(cls, '_unsupported_conditions', []))
    assert st.get_trades()


def test_undeclared_param_becomes_false_not_crash():
    """引用了 params 里没有的参数 → 恒假 + 记账（早期会在运行时 AttributeError 崩掉整轮）"""
    spec = _spec({'param': 'not_declared'})
    st, cls = _run(spec)
    notes = getattr(cls, '_unsupported_conditions', [])
    assert any('未在 params 里声明' in n for n in notes), notes
    assert st.get_trades() == []


def test_normal_param_form_still_works():
    """正常写法 {"param": "floor"} 不受影响"""
    spec = _spec({'param': 'floor'})
    st, cls = _run(spec)
    assert not [n for n in getattr(cls, '_unsupported_conditions', []) if '嵌套形态' in n]
    assert st.get_trades()

"""指标参数形态归一化：字典形态被当字面量会让回测**整轮卡死**。

真实事故（2026-10-14 定位）：schema 里示范 {"func":"ma","args":[{"data":"close"},5]}，
旧 _resolve_indicator 只认字符串形态，把 {"data":"close"} 当**字面量**塞进 bt 指标的
位置参数 → backtrader 把它包成 LineSeriesStub，而该 stub 的 _clock 缺失、_owner 又指回
指标自身 ⇒ Strategy._periodset（backtrader/strategy.py:168-180）上溯时钟链死循环：
真跑回测不报错、不返回，一次研发任务被挂死（此前被误判成「highest/lowest/mul 的问题」）。

本文件锁死三层：
① 字典形态与字符串形态编译等价（都能跑出成交）
② 畸形形态在编译期 / 保存期被拒（数组、未知数据名、无法识别的字典、mul 缺倍数或倍数非数值）
③ 真跑 cerebro 不卡死（子进程 + 超时，回归时不会挂死测试进程）
"""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _bars(n=60):
    """合成日线：缓慢上行 + 成交量波动，保证 ma / mul 都有意义"""
    dates = pd.date_range('2024-01-02', periods=n, freq='B')
    rows = []
    price = 10.0
    for i in range(n):
        price = price * (1 + (0.01 if i % 7 < 4 else -0.008))
        rows.append((price, price * 1.01, price * 0.99, price, 1_000_000 + (i % 20) * 50_000))
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'], index=dates)


def _spec(right, sell=None):
    return {
        'meta': {'strategy_id': 'arg_shape_probe', 'name': '参数形态探针'},
        'params': {},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'op': '>', 'left': {'data': 'close'}, 'right': right}]},
            'sell': sell or {'logic': 'AND', 'rules': [], 'hold_bars': 1},
        },
    }


def _run(spec):
    import backtrader as bt
    from backtest.json_compiler import compile_strategy

    cls = compile_strategy(json.dumps(spec, ensure_ascii=False))
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=_bars()))
    cerebro.addstrategy(cls, entry_timing='same_close', exit_timing='same_close')
    cerebro.broker.setcash(1_000_000)
    strat = cerebro.run()[0]
    return strat, cls


def test_dict_arg_runs_like_string_arg():
    """{"data": "close"} 与 "close" 必须等价：预热根数与成交笔数完全一致"""
    strat_dict, cls_dict = _run(_spec({'func': 'ma', 'args': [{'data': 'close'}, 5]}))
    strat_str, cls_str = _run(_spec({'func': 'ma', 'args': ['close', 5]}))
    assert strat_dict._minperiod == strat_str._minperiod == 5
    assert len(strat_dict.get_trades()) == len(strat_str.get_trades()) > 0
    for cls in (cls_dict, cls_str):
        assert not [n for n in getattr(cls, '_unsupported_conditions', []) if '参数形态不支持' in n]


def test_unknown_data_name_in_indicator_arg_falls_back_to_close():
    """指标参数里的未知数据名：回退 close + 记账（不静默、不崩）"""
    strat, cls = _run(_spec({'func': 'ma', 'args': [{'data': 'no_such_field'}, 5]}))
    assert any('未知' in n for n in getattr(cls, '_unsupported_conditions', []))
    assert strat.get_trades()


def test_undeclared_param_in_indicator_arg_becomes_zero():
    """指标参数引用未声明参数：按 0 处理 + 记账"""
    strat, cls = _run(_spec({'func': 'ma', 'args': [{'param': 'no_such_param'}, 5]}))
    assert any('未声明' in n for n in getattr(cls, '_unsupported_conditions', []))


def test_literal_first_arg_is_treated_as_close_data():
    """首位参数写成字面量/参数引用：必须按 close 处理（当 data line 传给 bt 会卡死整轮回测）"""
    strat, cls = _run(_spec({'func': 'ma', 'args': [0, 5]}))
    notes = getattr(cls, '_unsupported_conditions', [])
    assert any('第一个参数必须是数据引用' in n for n in notes), notes
    assert strat._minperiod == 5
    assert strat.get_trades()


def test_unrecognized_dict_arg_is_recorded_and_compiles():
    """无法识别的字典形态：记 unsupported + gap，回退 close（编译不抛）"""
    strat, cls = _run(_spec({'func': 'ma', 'args': [{'foo': 1}, 5]}))
    notes = getattr(cls, '_unsupported_conditions', [])
    assert any('参数形态不支持' in n for n in notes), notes
    assert any(g.get('kind') == 'indicator_arg' for g in getattr(cls, '_capability_gaps', []))


def test_mul_non_numeric_factor_is_false_not_always_true():
    """mul 倍数非数值：必须恒假（退化成 0 会变成「恒真」，条件静默失效）"""
    spec = _spec({'func': 'mul', 'args': [{'func': 'ma', 'args': ['close', 5]}, {'foo': 1}]})
    strat, cls = _run(spec)
    assert any('不是数值' in n for n in getattr(cls, '_unsupported_conditions', []))
    assert strat.get_trades() == []


def test_validator_accepts_all_supported_arg_shapes():
    from backtest.json_compiler import validate_strategy_json

    for right in (
        {'func': 'ma', 'args': [{'data': 'close'}, 5]},
        {'func': 'ma', 'args': ['close', 5]},
        {'func': 'ma', 'args': [{'value': 3}, {'value': 5}]},
        {'func': 'mul', 'args': [{'func': 'ma', 'args': ['volume', 20]}, 1.5]},
        {'func': 'highest', 'args': ['close', 20]},
    ):
        assert validate_strategy_json(json.dumps(_spec(right), ensure_ascii=False)) == [], right


def test_validator_rejects_malformed_args():
    from backtest.json_compiler import validate_strategy_json

    cases = [
        ({'func': 'ma', 'args': [['close'], 5]}, '形态不支持'),
        ({'func': 'ma', 'args': [{'data': 'no_such_field'}, 5]}, '不是可用数据引用'),
        ({'func': 'ma', 'args': [{'foo': 1}, 5]}, '无法识别的参数形态'),
        ({'func': 'mul', 'args': [{'func': 'ma', 'args': ['close', 5]}]}, '必须是 [<操作数>, 倍数]'),
        ({'func': 'mul', 'args': [{'func': 'ma', 'args': ['close', 5]}, 'x1.5']}, '必须是数值'),
    ]
    for right, needle in cases:
        errors = validate_strategy_json(json.dumps(_spec(right), ensure_ascii=False))
        assert any(needle in e for e in errors), (right, errors)


def test_dict_shape_does_not_hang_backtrader():
    """回归闸门：字典形态参数真跑 cerebro 不许卡死（子进程 + 超时兜底）。

    卡死时 backtrader 的 Strategy._periodset 会无限循环（不抛异常），
    所以只能在子进程里加超时观察，绝不能在测试进程内直接跑。
    """
    code = (
        'import json, sys\n'
        'sys.path.insert(0, r"' + str(PROJECT_ROOT) + '")\n'
        'import pandas as pd, backtrader as bt\n'
        'from backtest.json_compiler import compile_strategy\n'
        'dates = pd.date_range("2024-01-02", periods=60, freq="B")\n'
        'rows = [(10 + i * 0.01, 10.1 + i * 0.01, 9.9 + i * 0.01, 10 + i * 0.01, 1000000) for i in range(60)]\n'
        'df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)\n'
        'spec = {"meta": {"strategy_id": "hang_probe", "name": "hang"}, "params": {},\n'
        '        "conditions": {"buy": {"logic": "AND", "rules": [\n'
        '            {"type": "indicator", "op": ">", "left": {"data": "close"},\n'
        '             "right": {"func": "ma", "args": [{"data": "close"}, 5]}}]},\n'
        '            "sell": {"logic": "AND", "rules": [], "hold_bars": 1}}}\n'
        'cls = compile_strategy(json.dumps(spec, ensure_ascii=False))\n'
        'c = bt.Cerebro()\n'
        'c.adddata(bt.feeds.PandasData(dataname=df))\n'
        'c.addstrategy(cls, entry_timing="same_close", exit_timing="same_close")\n'
        'c.broker.setcash(1000000)\n'
        'st = c.run()[0]\n'
        'print("OK minperiod=" + str(st._minperiod) + " trades=" + str(len(st.get_trades())))\n'
    )
    proc = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                          timeout=150, cwd=str(PROJECT_ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert 'OK minperiod=' in proc.stdout, proc.stdout + proc.stderr[-1000:]

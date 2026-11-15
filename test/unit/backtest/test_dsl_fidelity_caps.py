"""DSL 能力补足：「N 日新高/新低」与「倍数」——用户原话必须能被忠实表达。

真实事故（2026-10-14 用户质询）：想法是
「收盘价创 20 日新高且成交量大于 20 日均量的 1.5 倍时买入，持有 5 根后卖出」，
但 8 轮生成出来的条件里 **没有任何一轮**是「20 日新高」（用的是布林上轨 / 高点均线），
**也没有任何一轮**带 1.5 倍门槛（只写了「高于均量」）。

根因不是模型偷懒，而是 DSL 当时**根本表达不了**这两件事：
- 没有 highest/lowest（N 日新高/新低）；
- 没有倍数运算（×1.5）。
所以模型只能改写条件 —— 用户看到的就是「随便生成了一个」。

本文件先锁**编译与取值**层（快、确定）；整条 cerebro 端到端跑通待补（见文件末尾说明）。
"""
import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.json_compiler import (_UNAVAILABLE, _resolve_ref, compile_strategy,
                                    validate_strategy_json)


def _spec(buy_rules, hold_bars=5):
    return {
        'meta': {'strategy_id': 'fidelity_probe', 'name': '原话保真探针'},
        'params': {'vol_n': {'value': 20, 'min': 5, 'max': 60}},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': buy_rules},
            'sell': {'logic': 'AND', 'rules': [], 'hold_bars': hold_bars},
        },
    }


def _compile(buy_rules):
    return compile_strategy(json.dumps(_spec(buy_rules), ensure_ascii=False))


class _StratStub:
    """最小策略替身：只提供 _resolve_ref 需要的东西（参数 / 字面量 / 数据行）"""

    def __init__(self, literals=None, params=None, lines=None):
        self._literals = literals or {}
        self.p = types.SimpleNamespace(**(params or {}))
        self.data = types.SimpleNamespace(**(lines or {}))
        self._bad_refs = set()


class _LineStub:
    def __init__(self, value, idx=5):
        self.array = [value]
        self.idx = idx

    def __getitem__(self, ago):
        return self.array[self.idx + ago]


def test_highest_lowest_are_registered():
    """highest / lowest 必须在指标表里，否则模型只能拿布林上轨凑「新高」"""
    from backtest.json_compiler import INDICATOR_MAP
    assert 'highest' in INDICATOR_MAP and 'lowest' in INDICATOR_MAP


def test_highest_compiles_without_deviation():
    cls = _compile([{'type': 'indicator', 'op': '>=', 'left': {'data': 'close'},
                     'right': {'func': 'highest', 'args': [{'data': 'high'}, 20]}}])
    assert not getattr(cls, '_unsupported_conditions', []), cls._unsupported_conditions
    assert validate_strategy_json(json.dumps(_spec([
        {'type': 'indicator', 'op': '>=', 'left': {'data': 'close'},
         'right': {'func': 'highest', 'args': [{'data': 'high'}, 20]}}]))) == []


def test_mul_builds_a_multiplied_ref():
    """成交量 > 20 日均量 × 1.5：编译成 mul: 包装引用（倍数进引用键，运行时参与比较）"""
    cls = _compile([{'type': 'indicator', 'op': '>', 'left': {'data': 'volume'},
                     'right': {'func': 'mul', 'args': [
                         {'func': 'ma', 'args': [{'data': 'volume'}, 20]}, 1.5]}}])
    assert not getattr(cls, '_unsupported_conditions', []), cls._unsupported_conditions


def test_mul_ref_resolves_to_multiplied_value():
    """mul: 引用的运行时要真的乘上去（不能退化成内层值）"""
    stub = _StratStub(params={'vol_n': 20}, lines={'volume': _LineStub(1000.0)})
    # 内层用字面量替身：val_x = 1000 → ×1.5 = 1500
    stub._literals = {'val_0': 1000.0}
    assert _resolve_ref('mul:val_0:1.5', stub) == pytest.approx(1500.0)
    assert _resolve_ref('mul:val_0:2', stub) == pytest.approx(2000.0)


def test_mul_of_unavailable_stays_unavailable():
    stub = _StratStub()
    stub._literals = {'val_0': 1000.0}
    stub._bad_refs = {'val_0'}
    assert _resolve_ref('mul:val_0:1.5', stub) is _UNAVAILABLE


def test_mul_bad_args_fail_safe():
    """mul 少参数 → 记账 + 恒假（不静默变成「只比较内层」）"""
    cls = _compile([{'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                     'right': {'func': 'mul', 'args': [{'data': 'close'}]}}])
    assert any('mul' in n for n in getattr(cls, '_unsupported_conditions', [])), \
        getattr(cls, '_unsupported_conditions', [])


# ⚠️ 运行时（cerebro）验证仍待补 —— 已缩小到「highest 这一支」：
# 编译/取值层 6 例全过；真跑 cerebro 的用例（highest / mul）会挂住，而同一天、
# 同一台机器上 test_operand_normalization.py 的 4 个 cerebro 用例 7 秒跑完。
# 已试并排除：带 volume 列、bt.indicators.Highest（换成自实现 RollingExtreme 照挂）、
# 卖出块写 "rules": []、next_open 口径。根因未定位前不写「已验证」。

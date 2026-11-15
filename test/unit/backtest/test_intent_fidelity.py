"""意图保真闸门单测（确定性提取 + LLM 核对 + fail-open）。"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config
from backtest.intent_fidelity import check_intent, describe_conditions

SPEC = {
    'conditions': {
        'buy': {'logic': 'AND', 'rules': [
            {'type': 'indicator', 'op': '>=', 'left': {'data': 'close'},
             'right': {'func': 'highest', 'args': [{'data': 'high'}, 20]}},
            {'type': 'indicator', 'op': '>', 'left': {'data': 'volume'},
             'right': {'func': 'mul', 'args': [
                 {'func': 'ma', 'args': [{'data': 'volume'}, 20]}, 1.5]}},
        ]},
        'sell': {'logic': 'AND', 'rules': [], 'hold_bars': 5},
    },
}


def test_describe_conditions_is_deterministic_and_readable():
    lines = describe_conditions(json.dumps(SPEC, ensure_ascii=False))
    assert lines[0] == '买点(AND): 数据:close >= highest(数据:high, 20)'
    assert lines[1] == '买点(AND): 数据:volume > ma(数据:volume, 20) × 1.5'
    assert lines[2] == '卖点: 持有 5 根后卖出'


def test_describe_handles_bad_json():
    assert describe_conditions('{oops') == []
    assert describe_conditions(None) == []


def test_check_intent_uses_llm(monkeypatch):
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_FIDELITY', True, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())
    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry',
                        lambda *a, **k: {'matches': False,
                                         'deviations': ['买点用了布林上轨，不是 20 日新高'],
                                         'summary': '买点偏离'})
    got = check_intent(idea='收盘价创 20 日新高且成交量大于 20 日均量的 1.5 倍时买入，持有 5 根后卖出',
                       entry_text='20 日新高 + 1.5 倍量', exit_text='持有 5 根', strategy_json=SPEC)
    assert got['checked'] is True and got['matches'] is False
    assert '布林上轨' in got['deviations'][0]
    assert got['conditions']


def test_check_intent_fail_open_on_llm_error(monkeypatch):
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_FIDELITY', True, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())

    def _boom(*a, **k):
        raise RuntimeError('llm down')

    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry', _boom)
    got = check_intent(idea='x', strategy_json=SPEC)
    assert got['checked'] is False and got['reason'] == 'llm_error', '核对不了必须记「未核对」，不能当通过'


def test_check_intent_switch_off_makes_zero_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_FIDELITY', False, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: calls.append(1) or object())
    got = check_intent(idea='x', strategy_json=SPEC)
    assert got['checked'] is False and calls == []


def test_check_intent_without_conditions(monkeypatch):
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_FIDELITY', True, raising=False)
    got = check_intent(idea='x', strategy_json={'conditions': {}})
    assert got['checked'] is False and got['reason'] == 'no_conditions'

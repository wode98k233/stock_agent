"""能力表 + 能力缺口（引擎脚手架化的第一步：知道模型想要什么）。"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.json_compiler import compile_strategy, export_capabilities


def test_capabilities_is_structured_and_complete():
    cap = export_capabilities()
    assert 'close' in cap['data_fields'] and 'volume' in cap['data_fields']
    for name in ('ma', 'rsi', 'boll', 'highest', 'lowest', 'volume'):
        assert name in cap['indicators'], name
    assert '>' in cap['operators'] and 'cross_above' in cap['operators']
    assert 'hold_bars' in cap['temporal'] and 'shift' in cap['temporal']
    assert set(cap['timings']) == {'next_open', 'same_close', 'next_close'}
    # 这三样正是 2026-10-14 那次「模型想要但引擎没有」的东西，必须常驻能力表
    assert 'highest' in cap['special_forms'] and 'mul' in cap['special_forms']


def test_unknown_indicator_records_gap_and_still_raises():
    """模型要一个引擎没有的指标 → 既报错（让它重试）也把缺口留下来"""
    spec = {'meta': {'strategy_id': 'gap_probe', 'name': 'p'}, 'params': {},
            'conditions': {'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'op': '>', 'left': {'data': 'close'},
                 'right': {'func': 'supertrend', 'args': [{'data': 'close'}, 10]}}]},
                'sell': {'logic': 'AND', 'rules': [], 'hold_bars': 3}}}
    gaps = []
    with pytest.raises(ValueError):
        compile_strategy(json.dumps(spec, ensure_ascii=False), gap_sink=gaps)
    assert gaps and gaps[0]['kind'] == 'indicator' and gaps[0]['name'] == 'supertrend', gaps


def test_unknown_data_field_records_gap():
    """模型要一个引擎没有的数据字段（如北向资金）→ 记缺口 + 该条件恒假"""
    spec = {'meta': {'strategy_id': 'gap_probe2', 'name': 'p'}, 'params': {},
            'conditions': {'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'op': '>', 'left': {'data': 'northbound_flow'},
                 'right': {'value': 0}}]},
                'sell': {'logic': 'AND', 'rules': [], 'hold_bars': 3}}}
    gaps = []
    cls = compile_strategy(json.dumps(spec, ensure_ascii=False), gap_sink=gaps)
    assert any(g['kind'] == 'data' and g['name'] == 'northbound_flow' for g in gaps), gaps
    assert getattr(cls, '_capability_gaps', []), '缺口也要挂在策略类上'


def test_known_capabilities_produce_no_gap():
    spec = {'meta': {'strategy_id': 'gap_probe3', 'name': 'p'}, 'params': {},
            'conditions': {'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'op': '>=', 'left': {'data': 'close'},
                 'right': {'func': 'highest', 'args': [{'data': 'high'}, 20]}},
                {'type': 'indicator', 'op': '>', 'left': {'data': 'volume'},
                 'right': {'func': 'mul', 'args': [
                    {'func': 'ma', 'args': [{'data': 'volume'}, 20]}, 1.5]}}]},
                'sell': {'logic': 'AND', 'rules': [], 'hold_bars': 5}}}
    gaps = []
    compile_strategy(json.dumps(spec, ensure_ascii=False), gap_sink=gaps)
    assert gaps == []

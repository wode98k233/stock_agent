"""AI 提议扩展引擎：只出方案，不执行、不自动生效。"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import capability_proposals as cp


@pytest.fixture()
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, '_proposals_dir', lambda: tmp_path)
    return tmp_path


LLM_OK = {
    'summary': '新增「N 日新高」指标',
    'definition': {'semantics': '过去 N 根 high 的最大值', 'formula': 'max(high[-N:])',
                   'data_source': 'high', 'params': [{'name': 'n', 'default': 20}]},
    'sample_rule_json': {'type': 'indicator', 'op': '>=', 'left': {'data': 'close'},
                         'right': {'func': 'highest', 'args': [{'data': 'high'}, 20]}},
    'engine_impact': 'INDICATOR_MAP 注册 highest',
    'risks': ['前视偏差'], 'test_plan': ['边界 N=1', '与手算一致'],
}


def test_propose_extension_parses_llm(monkeypatch):
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())
    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry', lambda *a, **k: LLM_OK)
    got = cp.propose_extension({'kind': 'indicator', 'name': 'highest', 'detail': '引擎没有'}, idea='创 20 日新高')
    assert got['ok'] is True
    p = got['proposal']
    assert p['status'] == 'pending_review' and p['name'] == 'highest'
    assert p['definition']['formula'] and p['test_plan']


def test_propose_extension_fail_open(monkeypatch):
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())

    def _boom(*a, **k):
        raise RuntimeError('llm down')

    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry', _boom)
    got = cp.propose_extension({'kind': 'data', 'name': 'northbound_flow'})
    assert got['ok'] is False and got['reason'] == 'llm_error'


def test_save_list_review_lifecycle(tmp_dir, monkeypatch):
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())
    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry', lambda *a, **k: LLM_OK)
    p = cp.propose_extension({'kind': 'indicator', 'name': 'highest'})['proposal']
    path = cp.save_proposal(p)
    assert Path(path).exists()
    assert [x['name'] for x in cp.list_proposals('pending_review')] == ['highest']
    res = cp.review_proposal(Path(path).name, 'approved', note='这个可以有')
    assert res['ok'] is True
    assert cp.list_proposals('pending_review') == []
    # 批准后只落「已批准定义」注册表，且显式标 effective=False（还没进引擎）
    reg = json.loads((tmp_dir / 'approved_registry.json').read_text(encoding='utf-8'))
    assert reg[0]['name'] == 'highest' and reg[0]['effective'] is False


def test_review_rejects_bad_decision(tmp_dir):
    assert cp.review_proposal('nope.json', 'maybe')['ok'] is False

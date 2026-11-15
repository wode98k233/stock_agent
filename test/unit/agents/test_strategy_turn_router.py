"""策略轮内路由的**契约**测试（不调真 LLM）。

锁三件事：

1. 阶段判定以 LLM 为主，正则只保留固定口令闭集 —— 用户换个说法（「那就试试呗」）
   不能被判成 confirm；
2. LLM 失败不许退化成「当普通提问放行」，必须回到阶段默认动作并明确追问
   （悄悄放行 = 用户以为回到分析模式，实际下一句又被会话截胡）；
3. 成交口径字段是**封闭枚举 + 买卖两侧独立**：非法值一律None（不许猜，
   口径猜错 = 回测结论整体失真），``timing_side`` 非法时调用方按买侧处理。
"""
import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """默认把 LLM 开关打开但工厂打桩 —— 各用例自己覆盖返回值"""
    from config import Config
    monkeypatch.setattr(Config, 'STRATEGY_TURN_ROUTER_LLM', True, raising=False)


def _stub_llm(monkeypatch, payload):
    """顶掉 llm_json_with_retry：返回固定 payload（dict/非 dict 都可）"""
    import utils.llm_factory as lf

    monkeypatch.setattr(lf, 'get_llm', lambda *a, **k: object())
    monkeypatch.setattr(lf, 'llm_json_with_retry', lambda *a, **k: payload)


def _route(session, text):
    from agents.shared.strategy_turn_router import route_session_turn
    return asyncio.run(route_session_turn(session, text))


SESSION = {'status': 'pending_confirm', 'run_uuid': '', 'idea': '尾盘买入次日卖出',
           'slots': {'universe': '600519', 'entry': '每天尾盘买',
                     'entry_timing': 'same_close', 'exit_timing': 'next_open'}}


# --------------------------------------------------------------------------
# 1. fast path 只是极小闭集
# --------------------------------------------------------------------------

def test_fast_path_only_fixed_phrases():
    from agents.shared.strategy_turn_router import fast_path_intent
    for phrase in ('开始', '开跑', '确认', '重跑', '结束会话', '跑到哪了'):
        assert fast_path_intent(phrase) is not None, phrase
    # 口语化说法**不许**命中 fast path —— 必须交给 LLM
    for phrase in ('那就试试呗', '行吧', '可以', '跑', '嗯'):
        assert fast_path_intent(phrase) is None, phrase


def test_fast_path_route_shape():
    out = _route(SESSION, '开始')
    assert out['intent'] == 'confirm' and out['source'] == 'fast_path'
    assert out['entry_timing'] is None and out['timing_side'] is None


# --------------------------------------------------------------------------
# 2. 兜底不许放行成普通提问
# --------------------------------------------------------------------------

def test_fallback_keeps_timing_fields():
    from agents.shared.strategy_turn_router import fallback_route
    out = fallback_route(SESSION)
    assert out['intent'] in ('confirm', 'amend', 'unrelated', 'progress')
    assert out['entry_timing'] is None and out['timing_side'] is None
    assert out['timing_only'] is False


def test_llm_failure_does_not_return_none(monkeypatch):
    """LLM 挂掉/超时 → 返回兜底 dict，绝不能是 None（None = 放行成普通提问）"""
    import utils.llm_factory as lf

    def _boom(*a, **k):
        raise RuntimeError('network down')

    # ⚠️ 必须用 monkeypatch（用例结束自动还原）：直接给模块属性赋值会永久污染
    # 进程内的 utils.llm_factory.get_llm —— 同 worker 后续用例（如
    # utils/memory.py:57 建 MemoryManager）会拿到 object()，
    # 抛 pydantic ValidationError（xdist 下表现为随机 flaky）。
    monkeypatch.setattr(lf, 'get_llm', lambda *a, **k: object())
    monkeypatch.setattr(lf, 'llm_json_with_retry', _boom)
    out = _route(SESSION, '那就试试呗')
    assert out is not None and out.get('intent')
    assert out['source'] == 'fallback'


def test_llm_rule_only_switch(monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'STRATEGY_TURN_ROUTER_LLM', False, raising=False)
    out = _route(SESSION, '那就试试呗')
    assert out['source'] == 'fallback_rule_only'


# --------------------------------------------------------------------------
# 3. 口径字段：封闭枚举 + 两侧独立
# --------------------------------------------------------------------------

def test_next_close_is_valid_timing(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'reason': '改卖出口径',
                            'entry_timing': 'next_close', 'timing_side': 'exit',
                            'timing_only': True})
    out = _route(SESSION, '卖点改成次日收盘')
    assert out['entry_timing'] == 'next_close'
    assert out['timing_side'] == 'exit'
    assert out['timing_only'] is True


def test_illegal_timing_becomes_none(monkeypatch):
    """口径猜错= 回测结论整体失真，所以非法值一律 None，绝不猜"""
    _stub_llm(monkeypatch, {'intent': 'amend', 'entry_timing': '明天开盘',
                            'timing_side': 'exit'})
    assert _route(SESSION, '卖点改成明天开盘')['entry_timing'] is None


def test_illegal_side_becomes_none(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'entry_timing': 'same_close',
                            'timing_side': 'both'})
    assert _route(SESSION, '买卖都改成尾盘')['timing_side'] is None


def test_illegal_intent_falls_back(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'think_hard'})
    assert _route(SESSION, '随便说点什么')['source'] == 'fallback'


def test_non_dict_llm_output_falls_back(monkeypatch):
    _stub_llm(monkeypatch, ['confirm'])
    # 「开始吧」在 FAST_PATH 闭集里，必须挑一个闭集外的句子才能走到 LLM 分支
    assert _route(SESSION, '那就开动吧')['source'] == 'fallback'


def test_slots_desc_contains_exit_timing():
    """口径两条都要进 prompt，否则 LLM 看不到 exit_timing 就会永远改买点"""
    import json as _json

    import agents.shared.strategy_turn_router as r
    desc = r._slots_desc(_json.dumps({'universe': '600519', 'entry_timing': 'same_close',
                                     'exit_timing': 'next_open'}))
    assert 'same_close' in desc and 'next_open' in desc


# --------------------------------------------------------------------------
# 4. 结构化修改（区间 / 标的）：只收明确给出的，非法值一律丢弃
# --------------------------------------------------------------------------

def test_slot_updates_are_parsed(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'reason': '改区间与标的',
                            'period_start': '2024-09-01', 'period_end': '2026-10-24',
                            'universe': '600519'})
    out = _route(SESSION, '区间改成2024年9月开始，标的换600519')
    assert out['period_start'] == '2024-09-01'
    assert out['period_end'] == '2026-10-24'
    assert out['universe'] == '600519'


def test_slot_updates_illegal_values_dropped(monkeypatch):
    """日期/标的解析不了就是 None：改错区间或标的，结论整体失真"""
    _stub_llm(monkeypatch, {'intent': 'amend', 'period_start': '2024年9月',
                            'period_end': '2024-13-01', 'universe': '创业板'})
    out = _route(SESSION, '区间改成2024年9月开始')
    assert out['period_start'] is None and out['period_end'] is None
    assert out['universe'] is None


def test_slot_updates_reversed_period_dropped(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'period_start': '2025-01-01',
                            'period_end': '2024-01-01'})
    out = _route(SESSION, '区间从2025年到2024年')
    assert out['period_start'] is None and out['period_end'] is None


# --------------------------------------------------------------------------
# 5. 标的范围：对话里说「沪深300 / 自选 / 板块 / 前 N 只」→ 规范 spec
# --------------------------------------------------------------------------

def test_universe_scope_is_parsed(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'universe_scope': {
        'source': 'index', 'target': '沪深300', 'limit': 50}})
    out = _route(SESSION, '范围换成沪深300前50只')
    assert out['universe_spec'] == 'POOL:index:000300|limit=50'
    assert out['universe'] is None


def test_universe_scope_board_and_kind(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'universe_scope': {
        'source': 'board', 'target': '医药', 'kind': 'etf'}})
    out = _route(SESSION, '换成医药板块只看ETF')
    assert out['universe_spec'] == 'POOL:board:医药|kind=etf'


def test_universe_scope_invalid_dropped(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'universe_scope': {'source': 'planet'}})
    assert _route(SESSION, '换个范围')['universe_spec'] is None


def test_universe_scope_single_code_is_not_a_scope(monkeypatch):
    """单个代码走 universe（单票研究），不能落成代码池。"""
    _stub_llm(monkeypatch, {'intent': 'amend',
                            'universe_scope': {'source': 'codes', 'target': '600519'}})
    assert _route(SESSION, '只跑600519')['universe_spec'] is None


def test_compare_exit_flag_parsed(monkeypatch):
    _stub_llm(monkeypatch, {'intent': 'amend', 'compare_exit': True})
    out = _route(SESSION, '开盘卖和收盘卖都对比一下')
    assert out['compare_exit'] is True
    _stub_llm(monkeypatch, {'intent': 'amend'})
    assert _route(SESSION, '改成 MACD 金叉')['compare_exit'] is False
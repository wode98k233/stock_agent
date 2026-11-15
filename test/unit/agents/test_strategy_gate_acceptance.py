'''策略会话闸门端到端验收（Plan 2 任务 9.1，离线等价断言）。

真 LLM / 真浏览器的手工剧本见计划步骤 9.2；本文件用 tmp 库 + 假研发入口，
只锁四条硬约束：不产生副作用、确认后才起 run、不误伤普通提问、分流题不建会话。

注：R2（任务 11）默认开启后基准带输入会真去调 LLM，故这里固定
STRATEGY_INTENT_LLM_FALLBACK=False（该开关由任务 11 加进 config.py，故 raising=False）。
'''
import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IDEA = '尾盘买入第二天卖出这个策略如何'


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    '''回测库指向 tmp 文件（范式同 test_strategy_session_store.py / test_strategy_gate.py）'''
    # trace 库同样隔离：闸门现在会为本轮建 run，不隔离会写进真实月库（污染月历）
    from utils.agent_trace import store as store_mod
    monkeypatch.setattr(store_mod.store, 'current_month_db',
                        lambda: str(tmp_path / 'agent_trace_acc.db'), raising=False)
    from config import Config
    test_db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_MODE', 'confirm', raising=False)
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_LLM_FALLBACK', False, raising=False)
    return test_db


def _gate(**kwargs):
    from agents.shared.strategy_gate import intercept_strategy_turn
    return asyncio.run(intercept_strategy_turn(**kwargs))


def _sessions(dialog_uuid=None):
    from utils.cache.backtest_db import get_backtest_db
    sql, params = 'SELECT * FROM strategy_session', ()
    if dialog_uuid:
        sql += ' WHERE dialog_uuid = ?'
        params = (dialog_uuid,)
    with get_backtest_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _runs():
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM strategy_run').fetchall()]


def _install_fake_entry(monkeypatch, calls):
    '''记录入参的假研发入口：断言「起 run 时带了什么」，真起线程的路径归 e2e'''
    import server.routes.strategy_research as sr

    def fake_start_strategy_run(idea, universe, period_start=None, period_end=None,
                                source='panel', dialog_uuid=None, entry_timing='next_open',
                                exit_timing=''):
        calls.append({'idea': idea, 'universe': universe, 'period_start': period_start,
                      'period_end': period_end, 'source': source,
                      'dialog_uuid': dialog_uuid, 'entry_timing': entry_timing,
                      'exit_timing': exit_timing})
        return {'run_uuid': 'sr-accept-0001', 'status': 'running'}

    monkeypatch.setattr(sr, 'start_strategy_run', fake_start_strategy_run)


def _answer_channel(answers):
    '''假澄清通道：校验 payload 契约后直接回答案（真前端见 server/static/js/app.js）'''
    def cb(payload):
        assert payload['reason'] == 'strategy_slots'
        assert payload['event_name'] == 'clarify_required'
        assert len(payload['questions']) == 4
        return {'answers': answers}
    return cb


def _with_channel(cb, fn):
    from agents.shared.budget_ctx import budget_decision_ctx
    token = budget_decision_ctx.set(cb)
    try:
        return fn()
    finally:
        budget_decision_ctx.reset(token)


def test_confirm_path_has_no_side_effect(tmp_backtest_db, monkeypatch):
    '''用例 1：命中策略但不确认 → 只留一行 pending_confirm，零 run；再调一次不重复开

    ⚠️ 第二次调用会委派给真实会话执行器。路由 LLM 在单测里被禁后回退成
    「confirm」，会让 pending 会话**真起一条池子线程**（写 output/pool_studies
    的失败记录，页面堆满测试残留）。这里把意图固定成 unknown，只验证「不重复开」。
    '''
    import agents.shared.strategy_turn_router as tr

    async def fake_route(session, text, logger=None):
        return {'intent': 'unknown'}

    monkeypatch.setattr(tr, 'route_session_turn', fake_route)
    out = _gate(user_input=IDEA, dialog_uuid='test-a', mode='react_stock')
    assert out and '开始' in out                      # 无交互通道 → 文本确认卡
    rows = _sessions('test-a')
    assert len(rows) == 1 and rows[0]['status'] == 'pending_confirm'
    assert _runs() == []
    out2 = _gate(user_input=IDEA, dialog_uuid='test-a', mode='react_stock')
    assert out2
    assert len(_sessions('test-a')) == 1               # 会话复用，不重复开
    assert _runs() == []


def test_run_starts_only_after_slots_answered(tmp_backtest_db, monkeypatch):
    '''用例 2：弹窗作答后会话转 active 并起 run（入参 = 用户答案）'''
    calls = []
    _install_fake_entry(monkeypatch, calls)
    answers = {'universe': '600519', 'entry': 'close > ma5',
               'exit': 'next_open', 'period': '1095'}
    out = _with_channel(_answer_channel(answers),
                        lambda: _gate(user_input=IDEA, dialog_uuid='test-c',
                                      mode='react_stock'))
    assert len(calls) == 1
    assert calls[0]['universe'] == '600519'
    assert calls[0]['period_start'] and calls[0]['period_end']
    # IDEA 原话「尾盘买入第二天卖出」→ 买卖口径各归各位：尾盘买 / 次日开盘卖。
    # 旧实现把「次日开盘卖出」的答案写进 entry_timing（问卖点却改买入口径），
    # 于是尾盘买被静默覆盖成次日开盘买 —— 这正是用户看到结果完全不符的根因之一。
    assert calls[0]['entry_timing'] == 'same_close'
    assert calls[0]['exit_timing'] == 'next_open'
    row = _sessions('test-c')[0]
    assert row['status'] == 'active'
    assert row['run_uuid'] == 'sr-accept-0001'          # 起跑后 run_uuid 写回会话
    assert row['entry_timing'] == 'same_close'
    assert '开跑' in out


def test_plain_question_is_not_intercepted(tmp_backtest_db):
    '''用例 3：普通分析提问零误伤（最重要的一条）'''
    out = _gate(user_input='茅台最近怎么样', dialog_uuid='test-b', mode='react_stock')
    assert out is None
    assert _sessions('test-b') == []
    assert _runs() == []


def test_condition_only_question_gets_disambiguation(tmp_backtest_db):
    '''用例 4：只含条件词 → 分流题，且不建会话（disambiguate 分支不落库）'''
    out = _gate(user_input='MACD 金叉是什么', dialog_uuid='test-d', mode='react_stock')
    assert out and '了解这个指标' in out and '回测一个用它的策略' in out
    assert _sessions('test-d') == []


def test_open_session_query_uses_index(tmp_backtest_db):
    '''SQL 断言：取活会话必须走 idx_ss_dialog（不是 SCAN）'''
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        plan = conn.execute(
            'EXPLAIN QUERY PLAN SELECT * FROM strategy_session WHERE dialog_uuid = ? '
            'AND status IN (?, ?) ORDER BY updated_at DESC LIMIT 1',
            ('x', 'pending_confirm', 'active')).fetchall()
    text = ' '.join(str(r[-1]) for r in plan)
    assert 'idx_ss_dialog' in text


def test_timeout_auto_starts(tmp_backtest_db, monkeypatch):
    '''弹窗超时 → 按已解析口径自动开跑（2026-10-25 用户拍板：两次交互就够）'''
    from agents.shared.clarify import CLARIFY_TIMEOUT
    import agents.shared.strategy_session_runner as ssr

    calls = []

    async def fake_pool(session):
        calls.append(session.get('universe'))
        session['pool_run_id'] = 'PR-TEST'
        return 'PR-TEST'

    monkeypatch.setattr(ssr, '_start_pool_async', fake_pool)
    out = _with_channel(lambda payload: CLARIFY_TIMEOUT,
                        lambda: _gate(user_input=IDEA, dialog_uuid='test-e',
                                      mode='react_stock'))
    assert calls and calls[0] == 'POOL:default'
    assert out and '不用再回「开始」' in out
    assert _sessions('test-e')[0]['status'] == 'active'
'''策略会话闸门单测（Plan 2 任务 4）。

隔离手段：backtest.db 指向 tmp_path；会话执行器（任务 5 的产物）用假模块顶掉。
每条用例都固定 STRATEGY_INTENT_LLM_FALLBACK=False —— R2 默认开启后，基准带（disambiguate）
会真去调 LLM，既慢又不确定；该开关由任务 11 才加进 config.py，故 raising=False。
'''
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def tmp_trace_db(tmp_path, monkeypatch):
    '''trace 库也指向临时文件：闸门现在会为「第一条策略消息」建 run，⛔ 不能写月库。

    不隔离的话这些用例会往 db/agent_trace/ 的当月库里塞满假 run，
    直接污染月历与 token 统计（范式同 test_strategy_trace.py）。
    '''
    from utils.agent_trace import store as store_mod
    db = str(tmp_path / 'agent_trace_gate.db')
    monkeypatch.setattr(store_mod.store, 'current_month_db', lambda: db, raising=False)
    return db


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch, tmp_trace_db):
    '''把回测库指向临时文件并建表（范式同 test_strategy_session_store.py）'''
    from config import Config
    test_db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_MODE', 'confirm', raising=False)
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_LLM_FALLBACK', False, raising=False)
    return test_db


def _install_fake_runner(monkeypatch, text='【策略会话】本轮由会话接管', calls=None):
    '''用假模块顶掉 agents.shared.strategy_session_runner（真模块由任务 5 建）'''
    mod = types.ModuleType('agents.shared.strategy_session_runner')

    async def run_session_turn(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return text

    mod.run_session_turn = run_session_turn
    monkeypatch.setitem(sys.modules, 'agents.shared.strategy_session_runner', mod)
    return text


def _gate(**kwargs):
    from agents.shared.strategy_gate import intercept_strategy_turn
    return asyncio.run(intercept_strategy_turn(**kwargs))


def _open(dialog_uuid='local'):
    from utils.cache.backtest_db import get_open_strategy_session
    return get_open_strategy_session(dialog_uuid)


def _count():
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        return conn.execute('SELECT COUNT(*) FROM strategy_session').fetchone()[0]

def _trace_runs(db):
    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute('SELECT * FROM runs ORDER BY created_at')]
    finally:
        con.close()


def _trace_steps(db, run_id):
    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            'SELECT * FROM steps WHERE run_id=? ORDER BY id', (run_id,))]
    finally:
        con.close()


def _with_channel(cb, fn):
    '''把假弹窗回调塞进 budget_decision_ctx（Web 通道），跑完还原'''
    from agents.shared.budget_ctx import budget_decision_ctx
    token = budget_decision_ctx.set(cb)
    try:
        return fn()
    finally:
        budget_decision_ctx.reset(token)


IDEA = '尾盘买入第二天卖出这个策略如何？'


def test_off_mode_returns_none(tmp_backtest_db, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_MODE', 'off')
    assert _gate(user_input=IDEA) is None
    assert _count() == 0


def test_empty_input_returns_none(tmp_backtest_db):
    assert _gate(user_input='   ') is None
    assert _count() == 0


def test_plain_question_returns_none(tmp_backtest_db):
    assert _gate(user_input='茅台最近怎么样') is None
    assert _count() == 0


def test_condition_only_returns_disambiguate_text(tmp_backtest_db):
    out = _gate(user_input='MACD 是什么')
    assert isinstance(out, str)
    assert '只是问问' in out
    assert _count() == 0


def test_confirm_mode_creates_pending_session(tmp_backtest_db):
    out = _gate(user_input=IDEA)
    assert isinstance(out, str)
    assert '开始' in out
    sess = _open()
    assert sess is not None
    assert sess['status'] == 'pending_confirm'
    assert sess['opened_by'] == 'explicit'
    assert sess['dialog_uuid'] == 'local'

def test_new_session_builds_one_trace_run(tmp_backtest_db, tmp_trace_db):
    '''第一条策略消息本身也是一次「策略执行」，必须在月历/执行过程里留下记录。

    P1 缺口：之前只有「委派给 run_session_turn」那条路会建 run —— 用户的第一句
    「尾盘买入第二天卖出这个策略如何」在建会话 + 弹确认卡这条路上完全没有 run，
    月历里这段消耗凭空消失。现在要求：恰好一条 run、有「建会话」step、
    且 run_id 发布给 agent_runner（否则本轮消息绑定不上执行过程）。
    '''
    from agents.shared.strategy_gate import intercept_strategy_turn
    from agents.shared.strategy_trace import last_turn_run_scope

    async def go():
        # ⚠️ 必须在同一个 task 内读 ContextVar：asyncio.run 会复制上下文，
        # 作用域内 set 的值回不到调用方（生产里 agent_runner 是同 task await）
        out = await intercept_strategy_turn(user_input=IDEA)
        return out, last_turn_run_scope.get()

    out, published = asyncio.run(go())
    assert isinstance(out, str) and out
    rows = _trace_runs(tmp_trace_db)
    assert len(rows) == 1, rows
    assert rows[0]['agent_name'] == 'strategy_session'
    assert rows[0]['status'] == 'success', 'run 必须收尾（停在 running 会让页面一直转圈）'
    assert rows[0]['id'] == published, 'run_id 要发布给 agent_runner'
    assert [s['step_name'] for s in _trace_steps(tmp_trace_db, rows[0]['id'])] == ['建会话']
    assert _count() == 1, '建 run 不能顺带多建会话'


def test_pending_session_short_circuits_intent(tmp_backtest_db, monkeypatch):
    calls = []
    text = _install_fake_runner(monkeypatch, calls=calls)
    _gate(user_input=IDEA)
    out = _gate(user_input='茅台最近怎么样')      # 有活会话 → 续轮，不判意图、不进报告
    assert out == text
    assert len(calls) == 1
    assert calls[0]['channel'] == 'text'        # 无弹窗通道（CLI/测试）
    assert _count() == 1


def test_archived_session_not_reopened(tmp_backtest_db, monkeypatch):
    _install_fake_runner(monkeypatch)
    _gate(user_input=IDEA)
    first = _open()
    from utils.cache.backtest_db import archive_strategy_session
    archive_strategy_session(first['session_uuid'])
    assert _open() is None
    _gate(user_input=IDEA)                      # 归档后同一句会重新判定并新建会话
    second = _open()
    assert second is not None
    assert second['session_uuid'] != first['session_uuid']
    assert _count() == 2


def test_auto_mode_with_complete_slots_goes_active(tmp_backtest_db, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_MODE', 'auto')
    _install_fake_runner(monkeypatch)
    _gate(user_input='600519 尾盘买入第二天卖出这个策略如何？')
    sess = _open()
    assert sess is not None
    assert sess['status'] == 'active'
    assert sess['universe'] == '600519'


def test_confirm_mode_without_universe_offers_default_pool(tmp_backtest_db, monkeypatch):
    """池子优先：没报代码时不再逼用户选一只，而是先给「默认池」确认卡。"""
    from config import Config
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_MODE', 'confirm')
    out = _gate(user_input='这个策略如何？')
    assert isinstance(out, str)
    assert '默认池' in out
    assert '开始' in out
    assert _open()['status'] == 'pending_confirm'


def test_card_payload_reason(tmp_backtest_db, monkeypatch):
    # 超时现在会直接开跑：必须顶掉执行器，否则这条用例会真起一个池子后台线程
    _install_fake_runner(monkeypatch)
    from agents.shared.clarify import CLARIFY_TIMEOUT
    seen = {}

    def cb(payload):
        seen.update(payload)
        return CLARIFY_TIMEOUT

    _with_channel(cb, lambda: _gate(user_input=IDEA))
    assert seen['reason'] == 'strategy_slots'
    assert seen['event_name'] == 'clarify_required'
    qs = seen['questions']
    assert [q['id'] for q in qs] == ['universe', 'entry', 'exit', 'period']
    for q in qs:
        assert q['recommended'] in [o['value'] for o in q['options']]


def test_card_timeout_auto_starts(tmp_backtest_db, monkeypatch):
    """弹窗超时不再要求第三次「开始」：按已解析口径直接开跑（用户 2026-10-25 拍板）。"""
    from agents.shared.clarify import CLARIFY_TIMEOUT
    _install_fake_runner(monkeypatch)
    out = _with_channel(lambda payload: CLARIFY_TIMEOUT, lambda: _gate(user_input=IDEA))
    assert isinstance(out, str)
    assert '不用再回「开始」' in out
    assert _open()['status'] == 'active'


def test_card_answers_activate(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_runner(monkeypatch, calls=calls)
    answers = {'answers': {'universe': '600519', 'entry': 'macd golden cross',
                           'exit': 'next_open', 'period': '365'}}
    _with_channel(lambda payload: json.dumps(answers), lambda: _gate(user_input=IDEA))
    # 答完弹窗必须用固定口令「开始」直接开跑：把用户原话再交给轮内路由会被判成
    # amend，又追加「补充条件」并重发确认卡，逼用户回第三次「开始」（真机 2026-10-25）。
    assert calls and calls[0]['user_input'] == '开始'
    sess = _open()
    assert sess['status'] == 'active'
    assert sess['universe'] == '600519'
    assert sess['entry_text'] == 'macd golden cross'
    # IDEA 原话是「尾盘买入第二天卖出」→ 买入口径 same_close；卡片问的是「次日怎么卖」，
    # 答案必须落``exit_timing``。旧实现把它写进了 ``entry_timing``：问卖点却改买入口径，
    # 且把用户原话里的尾盘买静默覆盖掉。
    assert sess['entry_timing'] == 'same_close'
    assert sess['exit_timing'] == 'next_open'


def test_card_cancel_raises(tmp_backtest_db):
    from utils.cancellation import AgentCancelled
    with pytest.raises(AgentCancelled):
        _with_channel(lambda payload: 'cancel', lambda: _gate(user_input=IDEA))
    assert _open()['status'] == 'pending_confirm'


def test_channel_exception_falls_back_to_text_card(tmp_backtest_db):
    '''弹卡通道自身故障时**不放行给报告模式**（否则策略问题会落进报告管线）。

    与计划用例 14 的原始期望（返回 None 放行）有意不同：改为回退文本确认卡，
    会话留在 pending_confirm，用户下一轮回「开始」仍能续上。
    '''
    def boom(payload):
        raise RuntimeError('SSE 通道挂了')

    out = _with_channel(boom, lambda: _gate(user_input=IDEA))
    assert isinstance(out, str)
    assert '开始' in out
    assert _open()['status'] == 'pending_confirm'


def test_card_questions_follow_user_words(tmp_backtest_db):
    '''题面与默认选项都要贴着用户原话。

    真实走查（2026-10-14 冒烟）：用户说「MACD 金叉买入，收盘价跌破 10 日均线卖出」，
    卡片却问「「第二天卖出」具体怎么卖？」，而且默认选项写死成「收盘价上穿 5 日均线」——
    卡片超时或用户直接点推荐，就会把用户自己说的买点**覆盖**成这个默认值。
    '''
    from agents.shared.strategy_gate import _slot_questions_template, _apply_answers
    verdict = {'text': 'MACD 金叉买入，收盘价跌破 10 日均线卖出', 'reason': 'explicit_imperative'}
    slots = {'universe': {'value': '600519', 'source': 'user'},
             'entry': {'value': 'MACD 金叉', 'source': 'user'},
             'exit': {'value': '收盘价跌破 10 日均线', 'source': 'user'}}
    qs = {q['id']: q for q in _slot_questions_template(verdict, slots)}
    assert 'MACD 金叉' in qs['entry']['question']
    assert qs['entry']['options'][0]['value'] == 'MACD 金叉'
    assert qs['entry']['recommended'] == 'MACD 金叉'
    assert '收盘价跌破 10 日均线' in qs['exit']['question']
    assert qs['exit']['options'][0]['value'] == '收盘价跌破 10 日均线'
    assert qs['exit']['recommended'] == '收盘价跌破 10 日均线'
    # 走推荐（=超时兜底）时不能改掉用户原话
    merged = _apply_answers(slots, {q['id']: q['recommended'] for q in qs.values()})
    assert merged['entry']['value'] == 'MACD 金叉'
    assert merged['exit']['value'] == '收盘价跌破 10 日均线'


def test_card_questions_fall_back_without_slots(tmp_backtest_db):
    '''没解析出买卖点时，题面保持原来的通用问法（不能出现空引号）'''
    from agents.shared.strategy_gate import _slot_questions_template
    qs = {q['id']: q for q in _slot_questions_template({'text': '这个策略如何？'}, {})}
    assert '什么条件算「买」' in qs['entry']['question']
    assert '第二天卖出' in qs['exit']['question']
    assert qs['entry']['options'][0]['value'] == 'close > ma5'

# --------------------------------------------------------------------------
# 确认卡：优先由 LLM 就着用户这句话现写（写死的模板只是兜底）
# --------------------------------------------------------------------------

def _llm_card_payload():
    return {'questions': [
        {'id': 'universe', 'question': '「MACD 金叉」先跑哪只？',
         'options': [{'label': '就 600519', 'value': '600519'},
                     {'label': '我持仓 + 自选', 'value': '__watchlist__'}],
         'recommended': '600519'},
        {'id': 'entry', 'question': '买点按「MACD 金叉」跑吗？',
         'options': [{'label': '就按 MACD 金叉', 'value': 'MACD 金叉'},
                     {'label': '改成 5/20 金叉', 'value': 'close > ma5'}],
         'recommended': 'MACD 金叉'},
        {'id': 'exit', 'question': '卖点按「收盘价跌破 10 日均线」跑吗？',
         'options': [{'label': '就按跌破 MA10', 'value': '收盘价跌破 10 日均线'}],
         'recommended': '收盘价跌破 10 日均线'},
        {'id': 'period', 'question': '跑多长时间？',
         'options': [{'label': '近两年', 'value': '730'}, {'label': '近一年', 'value': '365'}],
         'recommended': '730'},
    ]}


def test_slot_card_uses_llm_questions(monkeypatch):
    '''确认卡题面与选项都来自 LLM（写死模板会问出与想法无关的题）'''
    from config import Config
    from agents.shared import strategy_gate as sg
    monkeypatch.setattr(Config, 'STRATEGY_SLOT_CARD_LLM', True, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())
    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry',
                        lambda *a, **k: _llm_card_payload())
    qs = asyncio.run(sg._slot_questions(
        {'text': 'MACD 金叉买入，收盘价跌破 10 日均线卖出'}, {}, logger=None))
    assert [q['id'] for q in qs] == ['universe', 'entry', 'exit', 'period']
    assert 'MACD 金叉' in qs[1]['question']
    assert qs[1]['recommended'] == 'MACD 金叉'
    assert qs[2]['options'][0]['value'] == '收盘价跌破 10 日均线'


def test_slot_card_falls_back_when_llm_fails(monkeypatch):
    '''LLM 挂了也必须有卡：回退内置模板（策略轮不能因为生成卡片失败跑不动）'''
    from config import Config
    from agents.shared import strategy_gate as sg
    monkeypatch.setattr(Config, 'STRATEGY_SLOT_CARD_LLM', True, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())

    def _boom(*a, **k):
        raise RuntimeError('llm down')

    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry', _boom)
    qs = asyncio.run(sg._slot_questions({'text': '尾盘买入第二天卖出'}, {}, logger=None))
    assert [q['id'] for q in qs] == ['universe', 'entry', 'exit', 'period']
    assert qs[0]['recommended'] in [o['value'] for o in qs[0]['options']]


def test_slot_card_falls_back_on_partial_llm_json(monkeypatch):
    '''LLM 只给一半题目 → 整体回退模板（缺 id 会让那次作答静默丢失）'''
    from config import Config
    from agents.shared import strategy_gate as sg
    monkeypatch.setattr(Config, 'STRATEGY_SLOT_CARD_LLM', True, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: object())
    partial = {'questions': _llm_card_payload()['questions'][:2]}
    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry', lambda *a, **k: partial)
    qs = asyncio.run(sg._slot_questions({'text': 'x'}, {}, logger=None))
    assert '什么条件算「买」' in qs[1]['question'], '半张卡必须整体回退模板'


def test_slot_card_switch_off_makes_zero_llm_calls(monkeypatch):
    from config import Config
    from agents.shared import strategy_gate as sg
    calls = []
    monkeypatch.setattr(Config, 'STRATEGY_SLOT_CARD_LLM', False, raising=False)
    monkeypatch.setattr('utils.llm_factory.get_llm', lambda: calls.append(1) or object())
    qs = asyncio.run(sg._slot_questions({'text': 'MACD 金叉'}, {}, logger=None))
    assert calls == []
    assert [q['id'] for q in qs] == ['universe', 'entry', 'exit', 'period']

def test_universe_options_have_no_dead_ends(monkeypatch):
    """标的选项里不能留「点了就会被拒」的死路。

    2026-10-24 起范围走 `POOL:` spec：沪深300 以 `POOL:index:000300`（解析成成分股）
    出现，不再把指数码当单票；自选/持仓/板块都有真实后端实现。
    """
    import backtest.pool_scope as psmod
    monkeypatch.setattr(psmod, 'describe_scope', lambda spec: '默认池（25 股票 + 15 ETF）')
    monkeypatch.setattr(psmod, 'scope_short_label', lambda spec: '默认池')
    from agents.shared.strategy_gate import _SLOT_CARD_PROMPT, _slot_questions_template

    qs = {q['id']: q for q in _slot_questions_template({'text': '上升趋势尾盘买入'}, {})}
    values = [o['value'] for o in qs['universe']['options']]
    assert '000300' not in values
    assert '__watchlist__' not in values
    # 池子优先：默认池是推荐项；范围选项都对应真实后端实现（不是死路）
    assert qs['universe']['recommended'] == 'POOL:default'
    assert {'POOL:default', 'POOL:index:000300', 'POOL:watchlist',
            'POOL:portfolio', '600519'} <= set(values)
    assert '（值 000300）' not in _SLOT_CARD_PROMPT
    assert '__watchlist__' not in _SLOT_CARD_PROMPT


def test_resolve_slots_parses_scope_from_text():
    """原话里的范围（沪深300 / 自选 / 板块）要直接落成 POOL: spec。"""
    from agents.shared.strategy_gate import _resolve_slots
    slots = _resolve_slots({'text': '在沪深300成分股里验证深V反弹', 'slots': {}})
    assert slots['universe']['value'] == 'POOL:index:000300'
    assert slots['universe']['source'] == 'user'


def test_resolve_slots_single_code_beats_index_mention():
    """点名了一只票时，文里出现的「沪深300」只是基准，不能把范围改走。"""
    from agents.shared.strategy_gate import _resolve_slots
    slots = _resolve_slots({'text': '600519 相对沪深300 跑一个策略',
                           'slots': {'universe': '600519'}})
    assert slots['universe']['value'] == '600519'


def test_confirm_text_shows_resolved_scope(monkeypatch):
    import backtest.pool_scope as psmod
    monkeypatch.setattr(psmod, 'describe_scope', lambda spec: '沪深300 成分股（300 只）')
    from agents.shared.strategy_gate import build_confirm_text

    text = build_confirm_text({'slots_json': json.dumps({
        'universe': {'value': 'POOL:index:000300', 'source': 'user'},
        'period': {'value': ['2024-01-01', '2026-10-24'], 'source': 'user'},
        'entry': {'value': '深V反弹', 'source': 'user'},
        'exit': {'value': '持有3根', 'source': 'user'},
    }, ensure_ascii=False)})
    assert '沪深300 成分股（300 只）' in text


def test_resolve_slots_sets_compare_flag():
    """「开盘卖 or 收盘卖」要落成对比标记，而不是选一个口径跑。"""
    from agents.shared.strategy_gate import _resolve_slots
    slots = _resolve_slots({'text': '深V反弹，持有3天后开盘价或收盘价卖出',
                           'slots': {}})
    assert slots['compare_exit_timings'] == {'value': True, 'source': 'user'}


def test_compare_flag_only_for_pool():
    """单票研发只有一条口径，不能因为句子里有「开盘或收盘」就标成对比。"""
    from agents.shared.strategy_gate import _resolve_slots
    slots = _resolve_slots({'text': '600519 持有3天后开盘或收盘卖出',
                           'slots': {'universe': '600519'}})
    assert 'compare_exit_timings' not in slots


def test_resolve_slots_sets_hold_sweep():
    """「持有 3-5 个交易日」要落成持有期矩阵 [3,4,5]。"""
    from agents.shared.strategy_gate import _resolve_slots
    slots = _resolve_slots({'text': '深V反弹，持有3-5个交易日卖出', 'slots': {}})
    assert slots['hold_period_sweep'] == {'value': [3, 4, 5], 'source': 'user'}


def test_confirm_text_auto_started_does_not_ask_for_start():
    from agents.shared.strategy_gate import build_confirm_text
    text = build_confirm_text({'slots_json': json.dumps({
        'universe': {'value': 'POOL:default', 'source': 'default_pool'},
        'period': {'value': ['2024-09-01', '2026-10-25'], 'source': 'user'},
        'entry': {'value': '深V反弹', 'source': 'user'},
        'exit': {'value': '持有3-5根', 'source': 'user'},
    }, ensure_ascii=False)}, auto_started=True)
    assert '已按下面这套开始' in text
    assert '不用再回「开始」' in text
    assert '回一句「开始」我就跑' not in text


def test_timeout_auto_starts_without_third_confirm(monkeypatch):
    """弹窗超时要按已解析口径直接开跑：提问 → 确认反问两次就够。"""
    import agents.shared.strategy_gate as sg
    import agents.shared.strategy_session_runner as ssr

    calls = {}
    monkeypatch.setattr(sg, '_ask_via_channel', lambda *a, **k: 'timeout')

    async def fake_questions(*a, **k):
        return []

    async def fake_turn(**kw):
        calls['turn'] = kw
        return '池子验证已开始：PR-20261025120000-abc'

    monkeypatch.setattr(sg, '_slot_questions', fake_questions)
    monkeypatch.setattr(ssr, 'run_session_turn', fake_turn)
    monkeypatch.setattr('utils.cache.backtest_db.update_strategy_session',
                        lambda *a, **k: calls.update({'status': k.get('status')}))
    monkeypatch.setattr(
        'utils.cache.backtest_db.get_open_strategy_session',
        lambda dlg: {'session_uuid': 'ss-1', 'status': 'active', 'idea': '深V反弹',
                     'universe': 'POOL:default', 'slots_json': '{}'})

    session = {'session_uuid': 'ss-1', 'status': 'pending_confirm', 'idea': '深V反弹',
               'universe': 'POOL:default', 'slots_json': '{}'}
    verdict = {'text': '深V反弹', 'slots': {}, 'reason': 'test', 'confidence': 1.0}
    out = asyncio.run(sg._finish_new_session(
        session=session, verdict=verdict,
        slots={'universe': {'value': 'POOL:default', 'source': 'default_pool'}},
        text='深V反弹', dlg='dlg-1', mode_cfg='manual', logger=None, scope=None,
        progress_callback=None, cancel_check=None))
    assert calls.get('status') == 'active'
    assert calls.get('turn', {}).get('user_input') == '开始'
    assert '不用再回「开始」' in out


def test_confirm_text_shows_hold_sweep():
    from agents.shared.strategy_gate import build_confirm_text
    text = build_confirm_text({'slots_json': json.dumps({
        'universe': {'value': 'POOL:default', 'source': 'default_pool'},
        'period': {'value': ['2024-09-01', '2026-10-25'], 'source': 'user'},
        'entry': {'value': '深V反弹', 'source': 'user'},
        'exit': {'value': '持有3-5根', 'source': 'user'},
        'hold_period_sweep': {'value': [3, 4, 5], 'source': 'user'},
    }, ensure_ascii=False)})
    assert '持有期对比：3 / 4 / 5' in text


def test_normalize_universe_value_handles_prose():
    """确认卡把范围写成一段人话时，要收敛成可回测的 spec（真机 2026-10-25）。"""
    from agents.shared.strategy_gate import _normalize_universe_value
    raw = '扩大范围，优先用沪深300，个股ETF混合直到找到100只标的'
    assert _normalize_universe_value(raw) == 'POOL:mixed|limit=100'
    assert _normalize_universe_value('600519') == '600519'
    assert _normalize_universe_value('POOL:default') == 'POOL:default'
    assert _normalize_universe_value('随便扩大一点吧') == ''


def test_resolve_slots_named_targets_become_code_pool(monkeypatch):
    """点名的中文股名 / ETF 简称必须落成代码池，不能回退默认池（真机 2026-10-25）。"""
    from datetime import date
    from agents.shared.strategy_gate import _resolve_slots
    monkeypatch.setattr('backtest.pool_scope._lookup_name',
                        lambda n: {'剑桥科技': ('603083', '剑桥科技')}.get(n, ('', '')))
    slots = _resolve_slots(
        {'text': '标的用剑桥科技和半导体ETF，深V反弹后开盘买'}, today=date(2026, 10, 25))
    assert slots['universe']['value'] == 'POOL:codes:603083,512480'
    assert slots['universe']['source'] == 'user'


def test_confirm_text_lists_unresolved_targets():
    """认不出的名字要写在卡上，绝不能让用户以为它们已经在池子里。"""
    from agents.shared.strategy_gate import build_confirm_text
    text = build_confirm_text({'idea': '深V反弹', 'slots_json': json.dumps({
        'universe': {'value': 'POOL:default', 'source': 'default_pool',
                     'unresolved': ['量子ETF']},
        'period': {'value': ['2024-09-01', '2026-10-25'], 'source': 'user'},
    }, ensure_ascii=False)})
    assert '这些标的我没认出' in text and '量子ETF' in text
    assert '不会偷偷换成别的池子' in text


def test_force_universe_spec_keeps_named_codes():
    """LLM 写卡把范围推荐成混合池时，点名的代码池必须是第一项 + recommended。"""
    from agents.shared.strategy_gate import _force_universe_spec
    qs = [{'id': 'universe', 'question': 'q', 'options': [
        {'label': '沪深300 + ETF 混合', 'value': 'POOL:mixed'}],
        'recommended': 'POOL:mixed'}]
    out = _force_universe_spec(qs, {'universe': {'value': 'POOL:codes:603083,512480'}})
    assert out[0]['recommended'] == 'POOL:codes:603083,512480'
    assert out[0]['options'][0]['value'] == 'POOL:codes:603083,512480'
    assert out[0]['options'][0]['label'] == '就按你点名的 2 只'


def test_period_question_keeps_user_range():
    """用户原话给了绝对区间时，卡上的推荐值必须是它（不能被「近 3 年」覆盖）。"""
    from agents.shared.strategy_gate import _slot_questions_template
    slots = {
        'universe': {'value': 'POOL:default', 'source': 'default_pool'},
        'period': {'value': ['2024-09-01', '2026-10-25'], 'source': 'user'},
        'entry': {'value': '', 'source': 'missing'},
        'exit': {'value': '', 'source': 'missing'},
    }
    qs = _slot_questions_template({'text': '深V反弹'}, slots)
    period = next(q for q in qs if q['id'] == 'period')
    assert period['recommended'] == '2024-09-01~2026-10-25'
    assert period['options'][0]['value'] == '2024-09-01~2026-10-25'
    # LLM 卡把它折成「约 784 天」时也要强制回绝对区间
    from agents.shared.strategy_gate import _force_period_range
    llm_qs = [{'id': 'period', 'question': 'q',
               'options': [{'label': '2024年9月到现在（约784天）', 'value': '784'}],
               'recommended': '784'}]
    forced = _force_period_range(llm_qs, slots)
    assert forced[0]['recommended'] == '2024-09-01~2026-10-25'


def test_apply_answers_keeps_universe_when_unresolvable():
    from agents.shared.strategy_gate import _apply_answers
    slots = {'universe': {'value': 'POOL:default', 'source': 'default_pool'}}
    merged = _apply_answers(slots, {'universe': '随便扩大一点吧'})
    assert merged['universe']['value'] == 'POOL:default'


def test_apply_answers_keeps_user_words_for_opaque_option_values():
    """LLM 卡的内部代号（deep_v_next_open）不能覆盖用户原话里的买卖条件。"""
    from agents.shared.strategy_gate import _apply_answers
    slots = {'entry': {'value': '我第二天开盘买入', 'source': 'user'},
             'exit': {'value': '持有3-5个交易日卖出', 'source': 'user'}}
    merged = _apply_answers(slots, {'entry': 'deep_v_next_open',
                                    'exit': 'hold_3_5_compare_open_close'})
    assert merged['entry']['value'] == '我第二天开盘买入'
    assert merged['exit']['value'] == '持有3-5个交易日卖出'
    assert 'exit_timing' not in merged


def test_slot_questions_normalize_prose_universe():
    from agents.shared.strategy_gate import _validate_slot_questions
    raw = '扩大范围，优先用沪深300，个股ETF混合直到找到100只标的'
    payload = [
        {'id': 'universe', 'question': '范围？',
         'options': [{'label': '扩大', 'value': raw}], 'recommended': raw},
        {'id': 'entry', 'question': '买点？',
         'options': [{'label': 'a', 'value': 'a'}], 'recommended': 'a'},
        {'id': 'exit', 'question': '卖点？',
         'options': [{'label': 'b', 'value': 'b'}], 'recommended': 'b'},
        {'id': 'period', 'question': '区间？',
         'options': [{'label': 'c', 'value': '365'}], 'recommended': '365'},
    ]
    out = _validate_slot_questions(payload)
    assert out is not None
    assert out[0]['options'][0]['value'] == 'POOL:mixed|limit=100'
    assert out[0]['recommended'] == 'POOL:mixed|limit=100'


def test_maybe_set_pool_sweeps_after_range_fixed():
    """范围先被写成 prose、后来才落成池子时，仍要补上口径/持有期矩阵标记。"""
    from agents.shared.strategy_gate import _maybe_set_pool_sweeps
    slots = {'universe': {'value': 'POOL:mixed|limit=100', 'source': 'user'}}
    _maybe_set_pool_sweeps(
        slots, {'text': '深V反弹，持有3-5个交易日，开盘或收盘卖出'})
    assert slots['compare_exit_timings']['value'] is True
    assert slots['hold_period_sweep']['value'] == [3, 4, 5]


def test_confirm_text_shows_compare_flag():
    from agents.shared.strategy_gate import build_confirm_text
    text = build_confirm_text({'slots_json': json.dumps({
        'universe': {'value': 'POOL:default', 'source': 'default_pool'},
        'period': {'value': ['2024-01-01', '2026-10-24'], 'source': 'user'},
        'entry': {'value': '深V反弹', 'source': 'user'},
        'exit': {'value': '持有3根', 'source': 'user'},
        'compare_exit_timings': {'value': True, 'source': 'user'},
    }, ensure_ascii=False)})
    assert '卖出口径对比' in text and '次日开盘卖 vs 次日收盘卖' in text

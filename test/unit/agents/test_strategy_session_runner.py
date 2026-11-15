'''策略会话执行器与结果卡单测（Plan 2 任务 5）。

隔离手段：
- backtest.db 指向 tmp_path（范式同 test/unit/backtest/test_strategy_session_store.py）；
- 研发入口 server.routes.strategy_research 用假模块顶掉（真货会起后台线程 + 真导出 FastAPI）；
- 卡片数据全部由本文件直接写库构造，不联网、不调 LLM。
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

IDEA = '尾盘买入第二天卖出这个策略如何？'
SLOTS = {
    'universe': {'value': '600519', 'source': 'user'},
    'period': {'value': ['2023-01-01', '2025-01-01'], 'source': 'user'},
    'entry': {'value': 'close > ma5', 'source': 'user'},
    'exit': {'value': '次日开盘卖出', 'source': 'user'},
    'entry_timing': {'value': 'next_open', 'source': 'default'},
}


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    '''把回测库指向临时文件并建表'''
    from config import Config
    test_db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    monkeypatch.setattr(Config, 'STRATEGY_SESSION_MAX_TURNS', 12, raising=False)
    return test_db


def _install_fake_entry(monkeypatch, calls=None, res=None, error=None, timing_aware=False):
    '''用假模块顶掉 server.routes.strategy_research（假模块已在 sys.modules 时不会真导入父包）

    timing_aware=True 时入口带 entry_timing / exit_timing 形参（买卖口径是**两个独立字段**，
    真入口在 B 批之后就有），用于断言「换口径 → 确认 → 重跑」真的把新口径传下去了，
    且买/卖两侧没有互相串。
    '''
    mod = types.ModuleType('server.routes.strategy_research')

    def start_strategy_run(idea, universe, period_start=None, period_end=None,
                           source='panel', dialog_uuid=None, entry_timing=None,
                           exit_timing=None):
        if calls is not None:
            call = {'idea': idea, 'universe': universe, 'period_start': period_start,
                    'period_end': period_end, 'source': source,
                    'dialog_uuid': dialog_uuid}
            if timing_aware:
                call['entry_timing'] = entry_timing
                call['exit_timing'] = exit_timing
            calls.append(call)
        if error is not None:
            raise error
        return res or {'run_uuid': 'sr-fake-0001', 'task_id': 't1', 'period': 'p',
                       'events_url': '/api/strategy/runs/sr-fake-0001/events'}

    mod.start_strategy_run = start_strategy_run
    monkeypatch.setitem(sys.modules, 'server.routes.strategy_research', mod)
    return mod


def _install_route(monkeypatch, intent=None, hint='', timing_only=False, entry_timing=None,
                   timing_side=None, period_start=None, period_end=None, universe=None,
                   universe_spec=None, compare_exit=False):
    '''顶掉 LLM 轮内路由（strategy_turn_router.route_session_turn）。

    阶段判定已经交给 LLM（正则只留固定口令闭集做 fast path，见 router 模块头注释），
    单测不该依赖真实 LLM 调用。这里默认把 intent 指向测试显式给出的值；
    intent=None 时按固定口令表回退（覆盖「开始 / 重跑 / 结束会话 / 跑到哪了」）。

    ⚠️ fast path 必须与 router.FAST_PATH 同口径：这里刻意独立写一份而不是 import，
    这样改 router 的词表时不会被测试悄悄带着一起变（测试要锁住的是行为契约）。

    timing_side 用于锁「只改卖点时点」不会顺带改掉买入口径。
    '''
    from agents.shared import strategy_turn_router as router
    fast = {'开始': 'confirm', '开跑': 'confirm', '确认': 'confirm', '重跑': 'rerun',
            '重跑一次': 'rerun', '结束会话': 'close', '落地': 'close', '跑到哪了': 'progress'}

    async def fake_route(session, user_input, logger=None):
        got = intent
        if got is None:
            got = fast.get((user_input or '').strip().replace(' ', ''), 'unknown')
        return {'intent': got, 'source': 'test', 'confidence': 1.0,
                'llm_reason': '', 'hint': hint,
                'timing_only': timing_only, 'entry_timing': entry_timing,
                'timing_side': timing_side,
                'period_start': period_start, 'period_end': period_end,
                'universe': universe, 'universe_spec': universe_spec,
                'compare_exit': compare_exit}

    monkeypatch.setattr(router, 'route_session_turn', fake_route)


def _turn(session, user_input, dialog_uuid='local'):
    from agents.shared.strategy_session_runner import run_session_turn
    return asyncio.run(run_session_turn(session=session, user_input=user_input,
                                        dialog_uuid=dialog_uuid, channel='card'))


def _fetch(session_uuid='ss-test-0001'):
    from utils.cache.backtest_db import get_strategy_session
    return get_strategy_session(session_uuid)


def _new_session(**overrides):
    '''建一条会话（槽位走嵌套形状，create 会同时落扁平列与 slots_json）'''
    from utils.cache.backtest_db import create_strategy_session, update_strategy_session
    create_strategy_session('ss-test-0001', 'local', IDEA, idea=IDEA, opened_by='rule',
                            slots=dict(SLOTS))
    if overrides:
        update_strategy_session('ss-test-0001', **overrides)
    return _fetch()


def _run_row(run_uuid='sr-run-1', status='running', stop_reason=None):
    from utils.cache.backtest_db import create_strategy_run, update_strategy_run
    row = create_strategy_run(run_uuid, IDEA, '600519', '2023-01-01~2025-01-01',
                              source='panel', dialog_uuid='local')
    if status != 'running' or stop_reason:
        update_strategy_run(run_uuid, status=status, stop_reason=stop_reason or '')
    return row


def _add_iter(run_uuid='sr-run-1', round_no=1, is_s=0.9, oos_s=0.6, trades=20,
              win_rate=0.5, hypothesis='收盘价上穿 5 日均线'):
    from utils.cache.backtest_db import insert_strategy_iter
    return insert_strategy_iter(run_uuid, round_no, hypothesis, '{}', 'h' + str(round_no),
                                'diff', 1, None,
                                {'sharpe': is_s, 'cagr': 0.2, 'mdd': -0.15,
                                 'win_rate': 0.55, 'trades': 40},
                                {'sharpe': oos_s, 'cagr': 0.1, 'mdd': -0.2,
                                 'win_rate': win_rate, 'trades': trades},
                                0, 'accepted', 'fake')


def _add_health(run_uuid='sr-run-1', health='ok', detail=''):
    '''造一条 origin='research' 的回测记录（get_latest_research_backtest 的取数口径）'''
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result
    create_backtest_run('bt-' + run_uuid,
                        {'code': '600519', 'stock_name': '贵州茅台', 'strategy_id': 's1',
                         'start_date': '2023-01-01', 'end_date': '2025-01-01'},
                        origin='research', ref_id=run_uuid)
    save_backtest_result('bt-' + run_uuid, {'health': health, 'health_detail': detail,
                                            'total_return': 0.0})


def _card(text):
    marker = '```' + 'strategy-card'
    assert marker in text, text
    return json.loads(text.split(marker, 1)[1].split('```', 1)[0])


def test_missing_session_returns_friendly_text(tmp_backtest_db):
    out = _turn(None, '开始')
    assert '会话' in out and '丢' in out


def test_pending_confirm_starts_run_and_marks_active(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    out = _turn(_new_session(), '开始')
    assert len(calls) == 1
    assert calls[0]['source'] == 'panel' and calls[0]['dialog_uuid'] == 'local'
    assert calls[0]['universe'] == '600519' and calls[0]['period_start'] == '2023-01-01'
    assert 'entry_timing' not in calls[0]           # 入口没有这个形参，硬传会 TypeError
    assert '开跑了' in out and 'sr-fake-0001' in out
    row = _fetch()
    assert row['status'] == 'active' and row['run_uuid'] == 'sr-fake-0001'
    assert row['turn_count'] == 1


def test_amend_note_is_recorded_without_starting(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='amend')
    out = _turn(_new_session(), '换成 MACD 金叉')
    assert calls == []
    assert '记下了' in out and '开始' in out          # 重发确认卡
    row = _fetch()
    assert row['status'] == 'pending_confirm'
    assert json.loads(row['plan_json'])['amend_notes'] == ['换成 MACD 金叉']


def test_amend_then_confirm_runs_amended_idea(tmp_backtest_db, monkeypatch):
    '''计划偏差说明 6 的最小形态：改条件 → 确认 → 新 run 带补充条件'''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='amend')
    _turn(_new_session(), '换成 MACD 金叉')
    _install_route(monkeypatch, intent='confirm')
    out = _turn(_fetch(), '开始')
    assert len(calls) == 1
    assert 'MACD 金叉' in calls[0]['idea'] and '补充条件' in calls[0]['idea']
    row = _fetch()
    assert row['status'] == 'active' and 'MACD 金叉' in row['idea']
    assert '开跑了' in out


def test_amend_patches_period_into_slots_and_card(tmp_backtest_db, monkeypatch):
    '''改区间必须写回列并显示在卡上。

    真机事故：用户说「区间改成 2024 年 9 月」，卡一字不变地重发一遍，
    起跑参数仍是旧的推荐区间（period_start 取自列，不取 idea 文本）。
    '''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='amend',
                   period_start='2024-09-01', period_end='2026-10-24')
    out = _turn(_new_session(), '区间改成2024年9月份开始，结束时间到现在')
    assert calls == []
    row = _fetch()
    assert row['period_start'] == '2024-09-01' and row['period_end'] == '2026-10-24'
    assert json.loads(row['slots_json'])['period']['value'] == ['2024-09-01', '2026-10-24']
    assert '2024-09-01' in out                      # 卡上必须能看到新区间
    assert '补充条件' in out and '2024年9月份' in out

    _install_route(monkeypatch, intent='confirm')
    _turn(_fetch(), '开始')
    assert calls[0]['period_start'] == '2024-09-01'
    assert calls[0]['period_end'] == '2026-10-24'


def test_amend_patches_universe_into_run(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='amend', universe='600519')
    _turn(_new_session(), '标的换成 600519 单只跑')
    row = _fetch()
    assert row['universe'] == '600519'
    _install_route(monkeypatch, intent='confirm')
    _turn(_fetch(), '开始')
    assert calls[0]['universe'] == '600519'


def test_amend_patches_scope_into_run(tmp_backtest_db, monkeypatch):
    '''范围也能聊出来定：换成沪深300 后，列与卡都要更新。'''
    import backtest.pool_scope as psmod

    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    monkeypatch.setattr(psmod, 'describe_scope', lambda spec: '沪深300 成分股（300 只）')
    _install_route(monkeypatch, intent='amend', universe_spec='POOL:index:000300')
    out = _turn(_new_session(), '范围换成沪深300成分股')
    row = _fetch()
    assert row['universe'] == 'POOL:index:000300'
    assert json.loads(row['slots_json'])['universe']['value'] == 'POOL:index:000300'
    assert '沪深300 成分股' in out and '300 只' in out


def test_amend_sets_compare_exit_flag(tmp_backtest_db, monkeypatch):
    '''「开盘卖和收盘卖都对比」要落成标记，确认卡也要说明。'''
    from utils.cache.backtest_db import update_strategy_session

    _install_fake_entry(monkeypatch)
    _new_session(universe='POOL:default')
    slots = dict(SLOTS)
    slots['universe'] = {'value': 'POOL:default', 'source': 'default_pool'}
    update_strategy_session('ss-test-0001',
                            slots_json=json.dumps(slots, ensure_ascii=False))
    _install_route(monkeypatch, intent='amend', compare_exit=True)
    out = _turn(_fetch(), '开盘卖和收盘卖都对比一下')
    assert json.loads(_fetch()['slots_json'])['compare_exit_timings']['value'] is True
    assert '卖出口径对比' in out


def test_explicit_exit_timing_clears_compare_flag(tmp_backtest_db, monkeypatch):
    '''明确只按某一种卖法时，取消对比标记，避免继续跑两遍。'''
    from utils.cache.backtest_db import update_strategy_session

    _new_session()
    slots = dict(SLOTS)
    slots['compare_exit_timings'] = {'value': True, 'source': 'user'}
    update_strategy_session('ss-test-0001',
                            slots_json=json.dumps(slots, ensure_ascii=False))
    _install_route(monkeypatch, intent='amend', timing_only=True,
                   entry_timing='next_close', timing_side='exit')
    _turn(_fetch(), '只按次日收盘卖')
    row = _fetch()
    assert row['exit_timing'] == 'next_close'
    assert 'compare_exit_timings' not in json.loads(row['slots_json'])


def test_amend_text_scope_fallback_updates_universe(tmp_backtest_db, monkeypatch):
    """路由没抽到范围时，也从「范围换成…」这句话本身兜底解析（真机原话）。"""
    _install_fake_entry(monkeypatch)
    _install_route(monkeypatch, intent='amend')      # 路由没给 universe
    _turn(_new_session(universe='POOL:default'),
          '范围换成个股和ETF混合总数达到100就行')
    assert _fetch()['universe'] == 'POOL:mixed|limit=100'


def test_amend_text_sets_hold_sweep(tmp_backtest_db, monkeypatch):
    """改条件时说的「持有 3-5 天」也要补上持有期矩阵标记。"""
    from utils.cache.backtest_db import update_strategy_session

    _install_fake_entry(monkeypatch)
    _new_session(universe='POOL:default')
    slots = dict(SLOTS)
    slots['universe'] = {'value': 'POOL:default', 'source': 'default_pool'}
    update_strategy_session('ss-test-0001',
                            slots_json=json.dumps(slots, ensure_ascii=False))
    _install_route(monkeypatch, intent='amend')
    _turn(_fetch(), '持有3-5个交易日卖出')
    merged = json.loads(_fetch()['slots_json'])
    assert merged['hold_period_sweep']['value'] == [3, 4, 5]


def test_confirm_with_prose_universe_asks_instead_of_starting(tmp_backtest_db, monkeypatch):
    """老会话/异常答案把一段人话写进 universe 时，不许去撞单票入口（真机 2026-10-25）。"""
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _new_session(universe='扩大范围，优先用沪深300')
    _install_route(monkeypatch, intent='confirm')
    out = _turn(_fetch(), '开始')
    assert calls == []
    assert '还不能回测' in out and '直接说范围' in out
    assert _fetch()['status'] == 'pending_confirm'


def test_active_prose_universe_start_is_blocked(tmp_backtest_db, monkeypatch):
    """会话已被错误地置 active 时，起跑前仍要拦住不可回测的范围。"""
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _new_session(status='active', universe='扩大范围，优先用沪深300')
    _install_route(monkeypatch, intent='confirm')
    out = _turn(_fetch(), '开始')
    assert calls == []
    assert '还不能回测' in out


def test_amend_relative_period_falls_back_to_rules(tmp_backtest_db, monkeypatch):
    '''路由没抽到区间时，词表兜底仍要认「近1年」这类封闭说法（否则静默不生效）'''
    from datetime import date, timedelta

    _install_fake_entry(monkeypatch)
    _install_route(monkeypatch, intent='amend')          # LLM 没给 period 字段
    _turn(_new_session(), '区间改成近1年')
    row = _fetch()
    end = date.today()
    assert row['period_end'] == end.isoformat()
    assert row['period_start'] == (end - timedelta(days=365)).isoformat()


def test_amend_note_keeps_whole_sentence(tmp_backtest_db, monkeypatch):
    '''改条件原文不再在 120 字处被腰斩（真机：130 字的句子丢了「收益/胜率」半句）'''
    _install_fake_entry(monkeypatch)
    _install_route(monkeypatch, intent='amend')
    text = ('标的可以扩大一点，个股 ETF 也是要凑合的 区间改成2024年9月份开始就行了，'
            '结束时间到现在 成交口径次日开盘价买入，然后持有3-5个交易日的开盘价或收盘价卖出，'
            '我需要知道收益和胜率')
    _turn(_new_session(), text)
    notes = json.loads(_fetch()['plan_json'])['amend_notes']
    assert notes[-1] == text


def test_active_done_amend_goes_back_to_confirm(tmp_backtest_db, monkeypatch):
    '''跑完后改条件：回确认卡，不打断、也不改已跑完的结果'''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _run_row(status='done')
    _add_iter(round_no=1, oos_s=0.5, trades=20)
    _install_route(monkeypatch, intent='amend')
    out = _turn(_new_session(status='active', run_uuid='sr-run-1'), '加上成交额大于 20 日均量')
    assert calls == []
    assert '记下了' in out and '开始' in out
    row = _fetch()
    assert row['status'] == 'pending_confirm'
    assert '成交额大于 20 日均量' in row['idea']
    _install_route(monkeypatch, intent='confirm')
    out2 = _turn(_fetch(), '开始')                      # 确认后跑新 run
    assert len(calls) == 1 and '成交额大于 20 日均量' in calls[0]['idea']
    assert '开跑了' in out2


def test_active_busy_amend_does_not_reset_session(tmp_backtest_db, monkeypatch):
    '''跑动中不打断：改条件也只回进度卡，会话保持 active'''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _run_row(status='running')
    _add_iter(round_no=1, oos_s=0.5, trades=20)
    out = _turn(_new_session(status='active', run_uuid='sr-run-1'), '换成 MACD 金叉')
    assert calls == []
    assert '还在跑' in out
    assert _fetch()['status'] == 'active'


def test_result_card_shows_health_detail(tmp_backtest_db, monkeypatch):
    '''体检原因要进卡（get_latest_research_backtest 回 health_detail）'''
    _install_fake_entry(monkeypatch)
    _run_row(status='done')
    _add_iter(round_no=1, oos_s=0.5, trades=20)
    _add_health(health='data_gap', detail='缺口 5 根（2023-10-12~2023-10-18）')
    card = _card(_turn(_new_session(status='active', run_uuid='sr-run-1'), '跑完了吗'))
    assert card['health'] == 'data_gap'
    assert [n for n in card['notes'] if '缺口 5 根' in n]

def test_result_card_includes_eval_block(tmp_backtest_db, monkeypatch):
    '''样本内外评判进卡（Plan 3 任务 4）：判定只有一个来源 backtest_eval 表'''
    from utils.cache.backtest_db import save_backtest_eval
    _install_fake_entry(monkeypatch)
    _run_row(status='done')
    _add_iter(round_no=1, oos_s=0.5, trades=20)
    _add_health(health='ok')
    save_backtest_eval('bt-sr-run-1', {'verdict': 'ok', 'is_sharpe': 1.4, 'oos_sharpe': 1.2,
                                       'sharpe_ratio_io': 1.17, 'split_date': '2024-01-01'})
    card = _card(_turn(_new_session(status='active', run_uuid='sr-run-1'), '跑完了吗'))
    assert card['eval']['verdict'] == 'ok'
    assert card['eval']['is_sharpe'] == 1.4 and card['eval']['oos_sharpe'] == 1.2
    assert card['eval']['split_date'] == '2024-01-01'


def test_result_card_eval_pending_without_row(tmp_backtest_db, monkeypatch):
    '''没评判过就是 pending，不是报错（评判是后台补跑）'''
    _install_fake_entry(monkeypatch)
    _run_row(status='done')
    _add_iter(round_no=1, oos_s=0.5, trades=20)
    _add_health(health='ok')
    card = _card(_turn(_new_session(status='active', run_uuid='sr-run-1'), '跑完了吗'))
    assert card['eval']['verdict'] == 'pending'
    assert card['eval']['is_sharpe'] is None


def test_pending_confirm_without_confirm_word_repeats_card(tmp_backtest_db, monkeypatch):
    '''LLM 判不出意图时不猜：回该阶段默认动作 + 确认卡，绝不擅自开跑。

    （旧实现靠「我再想想」不含 CONFIRM_WORDS 来兜底；现在这个判断归 LLM，
      LLM 给不出结论时也不能默认当成确认 —— 跑错策略的代价远大于多问一句。）
    '''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='unknown', hint='这条我没太理解你想做什么。')
    out = _turn(_new_session(), '我再想想')
    assert calls == []
    assert '开始' in out
    assert _fetch()['status'] == 'pending_confirm'


def test_active_without_run_starts_once(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='confirm')
    out = _turn(_new_session(status='active'), '跑到哪了')
    assert len(calls) == 1
    assert '开跑了' in out and 'sr-fake-0001' in out
    assert _fetch()['run_uuid'] == 'sr-fake-0001'


def test_start_replies_with_progress_card(tmp_backtest_db, monkeypatch):
    '''起跑后立刻回一张进度卡（不是纯文本），且卡片围栏与正文之间必须有空行。

    空行不是格式洁癖：缺了它，收尾的 ``` 不独占一行 → Markdown 把正文吞进代码块
    → 前端 JSON.parse 失败、卡片不渲染、露出裸 JSON（真浏览器实测复现过）。
    '''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    out = _turn(_new_session(), '开始')
    card = _card(out)
    assert card['kind'] == 'progress' and card['run_uuid'] == 'sr-fake-0001'
    assert card['round_done'] == 0 and card['total_rounds'] == 8
    # 围栏块必须完整闭合且独占行：块内不得出现正文残留
    assert out.count('```') % 2 == 0
    fence_close = out.index('```', out.index('```') + 3)
    assert out[fence_close:fence_close + 4] == '```\n'
    # 起跑文案必须说清「会自己更新」，不许再让用户二次追问
    assert '实时更新' in out and '不用再问' in out


def test_active_running_returns_progress_card(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _run_row(status='running')
    _add_iter(round_no=1)
    _add_iter(round_no=2, hypothesis='加成交量过滤')
    out = _turn(_new_session(status='active', run_uuid='sr-run-1'), '跑到哪了')
    assert calls == []
    assert '还在跑' in out
    card = _card(out)
    assert card['kind'] == 'progress' and card['round_done'] == 2
    assert card['latest']['hypothesis'] == '加成交量过滤'


def test_active_done_returns_result_card(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _run_row(status='done')
    _add_iter(round_no=1, is_s=0.9, oos_s=0.6, trades=20, win_rate=0.5)
    _add_health(health='ok')
    out = _turn(_new_session(status='active', run_uuid='sr-run-1'), '跑完了吗')
    assert calls == []
    card = _card(out)
    assert card['kind'] == 'result' and card['run_uuid'] == 'sr-run-1'
    assert card['universe'] == '600519' and card['period'] == '2023-01-01~2025-01-01'
    assert card['oos']['sharpe'] == 0.6 and card['oos']['trades'] == 20
    assert card['health'] == 'ok'
    assert not [n for n in card['notes'] if '体检' in n]     # health=ok 时不写体检噪音


def test_zero_trades_notes_sample_shortage(tmp_backtest_db, monkeypatch):
    _install_fake_entry(monkeypatch)
    _run_row(status='done')
    _add_iter(round_no=1, oos_s=0.5, trades=0, win_rate=0.0)
    card = _card(_turn(_new_session(status='active', run_uuid='sr-run-1'), '跑完了吗'))
    assert card['wilson'] is None
    assert [n for n in card['notes'] if '样本不足' in n]


def test_thirty_one_trades_has_wilson_interval(tmp_backtest_db, monkeypatch):
    _install_fake_entry(monkeypatch)
    _run_row(status='done')
    _add_iter(round_no=1, is_s=0.5, oos_s=0.4, trades=31, win_rate=0.484)
    card = _card(_turn(_new_session(status='active', run_uuid='sr-run-1'), '跑完了吗'))
    win = card['wilson']
    assert win and win['n'] == 31
    assert 0.0 < win['low'] < win['high'] < 1.0
    assert not [n for n in card['notes'] if '样本不足' in n]


def test_pick_best_iter_skips_overfit_candidate(tmp_backtest_db):
    from agents.shared.strategy_session_runner import _pick_best_iter
    a = {'round': 1, 'hypothesis': 'A', 'compile_ok': 1,
         'is_metrics': {'sharpe': 0.7}, 'oos_metrics': {'sharpe': 0.3, 'trades': 20}}
    b = {'round': 2, 'hypothesis': 'B', 'compile_ok': 1,
         'is_metrics': {'sharpe': 0.5}, 'oos_metrics': {'sharpe': 0.4, 'trades': 20}}
    assert _pick_best_iter([a, b])['hypothesis'] == 'B'
    assert _pick_best_iter([]) is None


def test_pick_best_iter_enforces_engine_red_lines(tmp_backtest_db):
    '''红线三层与引擎 select_best 同口径（compile_ok / overfit_flag / OOS 短样本）'''
    from agents.shared.strategy_session_runner import _pick_best_iter
    good = {'round': 1, 'hypothesis': 'GOOD', 'compile_ok': 1, 'overfit_flag': 0,
            'is_metrics': {'sharpe': 0.5}, 'oos_metrics': {'sharpe': 0.4, 'trades': 20}}
    uncompiled = {'round': 2, 'hypothesis': 'NOCOMPILE', 'compile_ok': 0,
                  'oos_metrics': {'sharpe': 3.0, 'trades': 50}}
    flagged = {'round': 3, 'hypothesis': 'OVERFIT', 'compile_ok': 1, 'overfit_flag': 1,
               'oos_metrics': {'sharpe': 2.5, 'trades': 50}}
    thin = {'round': 4, 'hypothesis': 'THIN', 'compile_ok': 1,
            'oos_metrics': {'sharpe': 2.0, 'trades': 3}}
    assert _pick_best_iter([good, uncompiled, flagged, thin])['hypothesis'] == 'GOOD'
    # 全部不过红线：退回最后一轮（卡上仍由 overfit_notes/sample_notes 标注不足）
    assert _pick_best_iter([uncompiled, flagged, thin])['hypothesis'] == 'THIN'


def test_failed_run_reports_stop_reason(tmp_backtest_db, monkeypatch):
    '''失败态必须带真实错误 + 可执行的下一步，不许只回「原因：failed」。'''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _run_row(status='failed', stop_reason='编译失败：条件无法解析')
    out = _turn(_new_session(status='active', run_uuid='sr-run-1'), '跑到哪了')
    assert calls == []
    # stop_reason 非枚举值时本身就是原因文案，必须原样带出
    assert '编译失败' in out
    # 下一步要可执行（能给用户按的按钮），不能只报错不给路
    assert '你可以这样继续' in out


def test_archived_session_returns_archive_text(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    out = _turn(_new_session(status='archived'), '跑到哪了')
    assert calls == []
    assert '归档' in out


def test_turn_limit_returns_wrap_up(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls)
    _install_route(monkeypatch, intent='progress')
    out = _turn(_new_session(turn_count=12), '开始')
    assert calls == []
    assert '12' in out and '收个尾' in out


def test_confirm_and_amend_phrase_matching(tmp_backtest_db):
    '''阶段判定不再靠词表：只保留固定口令闭集（fast path），其余交给 LLM。

    这条测试锁的是「不再有 is_confirm/is_amend 这类模糊词表」这个契约本身：
    用户曾经明确指出——进入策略会话后，「现在处于什么阶段」应该让 LLM 判断，
    正则只配做「是不是策略研究」那一层的兜底。
    '''
    from agents.shared import strategy_session_runner as runner
    from agents.shared import strategy_turn_router as router
    # 词表判定函数已删除（防止有人又加回来）
    for gone in ('is_confirm', 'is_amend', 'is_close', 'is_rerun', 'is_process_question',
                 'CONFIRM_WORDS', 'AMEND_WORDS', 'CLOSE_WORDS', 'RERUN_WORDS'):
        assert not hasattr(runner, gone), gone + ' 不该再存在（阶段判定归 LLM）'
    # fast path 只认逐字照抄按钮的固定口令，且不含「跑 / 行 / 可以」这类高歧义短词
    assert router.fast_path_intent('开始') == 'confirm'
    assert router.fast_path_intent('  重跑 ') == 'rerun'
    assert router.fast_path_intent('结束会话') == 'close'
    for fuzzy in ('那就试试呗', '算了不弄了', '先缓缓', '开跑吧'):
        assert router.fast_path_intent(fuzzy) is None, fuzzy + ' 不该走 fast path'


def test_turn_router_fast_path_and_fallback(tmp_backtest_db, monkeypatch):
    '''router 自身：固定口令走 fast path，自由文本交给 LLM，LLM 挂了就回阶段默认动作。'''
    import asyncio
    from agents.shared import strategy_turn_router as router
    session = {'status': 'pending_confirm', 'slots_json': '{}', 'turn_count': 0,
               'run_uuid': ''}
    assert asyncio.run(router.route_session_turn(session, '开始'))['intent'] == 'confirm'
    # LLM 挂掉：不许猜，回到 pending_confirm 的默认动作并带 hint
    def boom(*a, **k):
        raise RuntimeError('llm down')
    monkeypatch.setattr('utils.llm_factory.get_llm', boom, raising=False)
    got = asyncio.run(router.route_session_turn(session, '那就按你说的办吧'))
    assert got['intent'] == 'confirm' and got['source'] == 'fallback'
    assert got['hint']
    # 非法意图值也不能被当成合法意图放行
    async def bad_llm(*a, **k):
        return {'intent': '随便编的'}
    monkeypatch.setattr('utils.llm_factory.llm_json_with_retry',
                        lambda llm, msgs, **k: bad_llm(), raising=False)
    assert asyncio.run(router.route_session_turn(session, '试试'))['source'] == 'fallback'


def test_render_card_block_is_parseable(tmp_backtest_db):
    from agents.shared.strategy_card import render_card_block
    block = render_card_block({'a': 1})
    assert block.startswith('```strategy-card') and block.rstrip().endswith('```')
    assert json.loads(block.split('\n', 1)[1].rsplit('```', 1)[0]) == {'a': 1}


def test_emit_tolerates_legacy_callback(tmp_backtest_db):
    from agents.shared.strategy_session_runner import _emit
    got = []

    def legacy(event_type, message):
        got.append((event_type, message))

    _emit(legacy, '跑到第 3 轮了')                  # 只吃 (type, text) 的老回调不许抛
    assert got == [('milestone', '跑到第 3 轮了')]
    seen = []
    _emit(lambda event: seen.append(event), '第 4 轮')
    assert getattr(seen[0], 'message', None) == '第 4 轮'   # 优先发真 ProgressEvent

def test_card_actions_carry_fill_text(tmp_backtest_db):
    '''结果卡每个按钮都要带 fill（模块打不开时退回「填进输入框」），且与词表对得上

    2026-10-14 起按钮分两类：带 nav 的直跳模块（kline / lifecycle / research），
    其余仍只填字；两类都必须有 fill 兜底。
    '''
    from agents.shared.strategy_card import build_result_card
    card = build_result_card(run={'run_uuid': 'sr-1', 'idea': IDEA, 'universe': '600519',
                                  'period': '2023-01-01~2025-01-01'})
    acts = {a['id']: a for a in card['actions']}
    assert {'rerun_timing', 'add_condition', 'ledger'} <= set(acts)
    assert acts['ledger'].get('nav') == 'lifecycle' and acts['kline'].get('nav') == 'kline'
    assert all(a.get('fill') for a in card['actions'])
    # 半句话结尾，等用户把条件补完再回车（按钮不自动发送）
    assert acts['add_condition']['fill'].endswith('：')
    # 与 is_amend 词表对齐，否则填进去的话不会被当成「改条件」
    assert '再加' in acts['add_condition']['fill']
    # 与 is_timing_switch 词表对齐
    assert '口径' in acts['rerun_timing']['fill'] and '换' in acts['rerun_timing']['fill']
    # 与 is_process_question 词表对齐
    assert '研发过程' in acts['ledger']['fill']


def test_timing_switch_from_done_flips_and_asks_confirm(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls, timing_aware=True)
    _run_row(status='done')
    _add_iter(run_uuid='sr-run-1', round_no=1)
    _new_session(status='active', run_uuid='sr-run-1', entry_timing='next_open')
    _install_route(monkeypatch, intent='amend', timing_only=True, entry_timing='same_close')
    out = _turn(_fetch(), '换个成交口径再跑一次')
    row = _fetch()
    assert row['status'] == 'pending_confirm' and row['entry_timing'] == 'same_close'
    assert '当日收盘价成交' in out and '不覆盖' in out
    assert calls == []                       # 只改口径，确认前不起跑


def test_timing_switch_then_confirm_reruns_with_new_timing(tmp_backtest_db, monkeypatch):
    calls = []
    _install_fake_entry(monkeypatch, calls=calls, timing_aware=True)
    _run_row(status='done')
    _new_session(status='active', run_uuid='sr-run-1', entry_timing='next_open')
    _install_route(monkeypatch, intent='amend', timing_only=True, entry_timing='same_close')
    _turn(_fetch(), '换个成交口径再跑一次')
    _install_route(monkeypatch, intent='confirm')
    _turn(_fetch(), '开始')
    assert len(calls) == 1 and calls[0]['entry_timing'] == 'same_close'


def test_timing_switch_ignored_while_running(tmp_backtest_db, monkeypatch):
    '''跑动中不打断：换口径只回进度卡，会话状态与口径都不变'''
    _install_fake_entry(monkeypatch, calls=[], timing_aware=True)
    _run_row(status='running')
    _new_session(status='active', run_uuid='sr-run-1', entry_timing='next_open')
    _install_route(monkeypatch, intent='amend', timing_only=True, entry_timing='same_close')
    out = _turn(_fetch(), '换个成交口径再跑一次')
    row = _fetch()
    assert row['status'] == 'active' and row['entry_timing'] == 'next_open'
    assert '还在跑' in out


# --------------------------------------------------------------------------
# 买卖口径分离（B 批）：改卖点不得连带改买点
# --------------------------------------------------------------------------

def test_timing_side_exit_only_changes_exit(tmp_backtest_db, monkeypatch):
    '''「卖点改成次日收盘」只动 exit_timing，买入口径保持尾盘不变。

    买卖口径是两个独立字段。若只改 exit 就把 entry 一起改掉，用户原话
    「尾盘买入、次日卖出」会在换一次口径后彻底跑偏。
    '''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls, timing_aware=True)
    _run_row(status='done')
    _add_iter(run_uuid='sr-run-1', round_no=1)
    _new_session(status='active', run_uuid='sr-run-1',
                 entry_timing='same_close', exit_timing='next_open')
    _install_route(monkeypatch, intent='amend', timing_only=True,
                   entry_timing='next_close', timing_side='exit')
    _turn(_fetch(), '卖点改成次日收盘')
    row = _fetch()
    assert row['entry_timing'] == 'same_close', '买入口径被连带改掉了'
    assert row['exit_timing'] == 'next_close'
    assert calls == []


def test_timing_side_entry_only_changes_entry(tmp_backtest_db, monkeypatch):
    '''反向：只改买点时，卖点必须保持原值'''
    _install_fake_entry(monkeypatch, calls=[], timing_aware=True)
    _run_row(status='done')
    _add_iter(run_uuid='sr-run-1', round_no=1)
    _new_session(status='active', run_uuid='sr-run-1',
                 entry_timing='next_open', exit_timing='next_close')
    _install_route(monkeypatch, intent='amend', timing_only=True,
                   entry_timing='same_close', timing_side='entry')
    _turn(_fetch(), '买点改成尾盘')
    row = _fetch()
    assert row['entry_timing'] == 'same_close'
    assert row['exit_timing'] == 'next_close', '卖出口径被连带改掉了'


def test_exit_timing_is_passed_to_run(tmp_backtest_db, monkeypatch):
    '''起跑时两个口径都要传下去（漏一个就退化成买卖同口径）'''
    calls = []
    _install_fake_entry(monkeypatch, calls=calls, timing_aware=True)
    _new_session(status='active', entry_timing='same_close', exit_timing='next_close')
    _install_route(monkeypatch, intent='confirm')
    _turn(_fetch(), '开始')
    assert len(calls) == 1
    assert calls[0]['entry_timing'] == 'same_close'
    assert calls[0]['exit_timing'] == 'next_close'


def test_process_question_returns_iteration_summary(tmp_backtest_db, monkeypatch):
    _run_row(status='done')
    _add_iter(run_uuid='sr-run-1', round_no=1, oos_s=0.6, hypothesis='收盘价上穿 5 日均线')
    _add_iter(run_uuid='sr-run-1', round_no=2, oos_s=1.1, hypothesis='叠加 MACD 金叉')
    _new_session(status='active', run_uuid='sr-run-1')
    _install_route(monkeypatch, intent='summary')
    out = _turn(_fetch(), '看看这个策略的研发过程')
    assert '一共 2 轮' in out
    assert '第 1 轮' in out and '收盘价上穿 5 日均线' in out
    assert '第 2 轮' in out and '叠加 MACD 金叉' in out
    assert _fetch()['status'] == 'active'    # 看过程不改变会话状态

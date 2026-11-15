"""策略回测**独立 trace 执行上下文**的单测（C 批）。

为什么这些契约必须锁死：

1. **策略轮要有自己的 run**。之前 run 由 LangGraph 建，而策略轮压根不进图 →
   研发期 8 轮 LLM 调用在月历/token 统计里凭空消失（它恰恰是最贵的一段）。
2. **归属必须显式绑定**，不能「猜最近一条 run」。分库跨月回退会命中文档性陈旧
   running 残留行 → 过程流显示上一次/上个月任务的执行过程，token 也被错记。
3. **业务绝不能被埋点拖垮**：trace 建不起来时照常执行，且 handler 只跑一次
   （跑两次 = 一轮对话变两轮、起两个 run）。
4. **异常路径必须收尾**：run 停在 running 会让页面一直转圈，且下次「猜最近 run」
   还会命中它。

全部用独立的临时 trace 库，**不写月库**（否则测试会污染月历/token 统计）。
"""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def trace_db(tmp_path, monkeypatch):
    """把 trace 库指向临时文件（⛔ 绝不能用月库，测试数据会污染月历统计）。

    ``TraceRecorder.__init__`` 支持 ``db_path``，所以只需把 ``strategy_trace``
    里的构造点改成可注入；这里用最轻的方式：打桩 ``store.current_month_db``
    （TraceRecorder 在 db_path=None 时正是走它）。
    """
    from utils.agent_trace import store as store_mod

    db = str(tmp_path / 'agent_trace_probe.db')
    monkeypatch.setattr(store_mod.store, 'current_month_db', lambda: db, raising=False)
    yield db


def _runs(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute('SELECT * FROM runs')]
    finally:
        con.close()


def _steps(db, run_id):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            'SELECT * FROM steps WHERE run_id=? ORDER BY id', (run_id,))]
    finally:
        con.close()


# --------------------------------------------------------------------------
# 1. 会话轮：自建 run + 显式绑定
# --------------------------------------------------------------------------

def test_turn_scope_creates_run_and_binds(trace_db):
    from agents.shared.strategy_trace import strategy_turn_scope
    session = {'session_uuid': 'ss-t1', 'idea': '尾盘买入次日卖出'}
    with strategy_turn_scope(session, '开始') as scope:
        assert scope.run_id
        assert scope.created is True
    assert session['trace_run_id'] == scope.run_id, 'run_id 必须写回 session（显式绑定）'
    rows = _runs(trace_db)
    assert len(rows) == 1 and rows[0]['status'] == 'success'


def test_turn_scope_reuses_run_for_same_session(trace_db):
    """续轮沿用首个 run —— 否则一个会话被切成 20 个 run，执行过程没法看"""
    from agents.shared.strategy_trace import strategy_turn_scope
    session = {'session_uuid': 'ss-t2', 'idea': 'x'}
    with strategy_turn_scope(session, '第一轮') as s1:
        first = s1.run_id
    with strategy_turn_scope(session, '第二轮') as s2:
        assert s2.run_id == first
        assert s2.created is False
    assert len(_runs(trace_db)) == 1


def test_trace_guard_publishes_run(trace_db):
    """闸门侧靠ContextVar 拿 run_id（不改 intercept_strategy_turn 的返回契约）"""
    from agents.shared.strategy_session_runner import _TraceGuard
    from agents.shared.strategy_trace import last_turn_run_scope
    last_turn_run_scope.set('')
    session = {'session_uuid': 'ss-t3', 'idea': 'x'}
    guard = _TraceGuard(session, '开始')
    guard.close()
    assert last_turn_run_scope.get() == session['trace_run_id'] != ''


def test_attached_recorder_sets_active_recorder(trace_db):
    '''续轮/研发线程必须重挂 recorder：_active_recorder 是 ContextVar，不跨轮跨线程。

    这是「有 run、token 恒 0」的直接原因 —— 不挂就没有 configure hook，
    该上下文里所有 LLM 调用都不会被埋点。
    '''
    from utils.agent_trace.core import _active_recorder
    from agents.shared.strategy_trace import _attached_recorder
    assert _active_recorder.get() is None
    with _attached_recorder('strategy_research', 'run-x') as rec:
        assert _active_recorder.get() is rec
    assert _active_recorder.get() is None, '退出必须还原（否则污染调用方的埋点）'


def test_attached_recorder_writes_into_existing_run(trace_db):
    '''attach_run：只往既有 run 里写 step，绝不另插 runs 行、也不负责收尾。

    多插一条 runs 行 = 同一次任务在月历/运行列表里被算两次；
    收尾则必须留给建 run 的那段上下文（否则 run 会被提前 end）。
    '''
    from utils.agent_trace.core import TraceRecorder
    from agents.shared.strategy_trace import _attached_recorder
    owner = TraceRecorder(agent_name='strategy_session')
    run_id = owner.start_run({'input': '尾盘买入次日卖出'})
    owner.end_run()

    with _attached_recorder('strategy_research', run_id) as rec:
        assert rec._attached is True
        rec._create_step(run_id, None, 'strategy', '第1轮-gen', {}, {'label': '第1轮-gen'})
        rec._finish_step(run_id)
        # 链根回调（parent_run_id=None）也不能再插一条 run：旧判定只看 _run_created
        rec.on_chain_start({'name': 'probe'}, {'q': 1}, run_id='chain-root-1')
        rec.on_chain_end({'a': 1}, run_id='chain-root-1')

    rows = _runs(trace_db)
    assert len(rows) == 1, 'attach 不许再插 runs 行'
    assert rows[0]['id'] == run_id and rows[0]['status'] == 'success'
    assert '第1轮-gen' in [s['step_name'] for s in _steps(trace_db, run_id)]


def test_router_llm_runs_inside_trace_scope(trace_db, monkeypatch):
    '''阶段路由（本轮唯一的 LLM 调用）必须在 trace 作用域内。

    之前 route_session_turn 排在 _run_in_trace 之前 ⇒ 每一轮「有 run、token 恒 0」。
    这里用假路由断言「调用时 recorder 已挂上」，不依赖真 LLM。
    '''
    from agents.shared import strategy_session_runner as runner
    import agents.shared.strategy_turn_router as router_mod
    seen = {}

    async def fake_route(session, user_input, logger=None):
        from utils.agent_trace.core import _active_recorder
        seen['recorder'] = _active_recorder.get()
        return {'intent': 'unrelated'}

    monkeypatch.setattr(router_mod, 'route_session_turn', fake_route)
    session = {'session_uuid': 'ss-t9', 'idea': '尾盘买入次日卖出',
               'status': 'pending_confirm'}
    out = asyncio.run(runner._run_in_trace(
        session, '这个策略如何',
        lambda: runner._dispatch_turn(
            session=session, user_input='这个策略如何', dialog_uuid='dlg-1',
            progress_callback=None, logger=None, status='pending_confirm',
            turns=1, limit=12)))
    assert seen.get('recorder') is not None, '路由 LLM 必须在 trace 作用域内'
    assert isinstance(out, str) and out


def test_trace_guard_runs_handler_once(trace_db):
    """⛔ handler 绝不能跑两次（跑两次 = 一轮对话变两轮、起两个 run）"""
    from agents.shared import strategy_session_runner as runner
    calls = []

    async def handler():
        calls.append(1)
        return 'ok'

    session = {'session_uuid': 'ss-t4', 'idea': 'x'}

    async def go():
        return await runner._run_in_trace(session, '开始', handler)

    assert asyncio.run(go()) == 'ok'
    assert len(calls) == 1


def test_trace_failure_does_not_block_business(trace_db, monkeypatch):
    """trace 建不起来时照常执行（埋点是可观测性，不是业务依赖）"""
    from agents.shared import strategy_session_runner as runner
    import agents.shared.strategy_trace as st

    def _boom(*a, **k):
        raise RuntimeError('trace lib missing')

    monkeypatch.setattr(st, 'strategy_turn_scope', _boom, raising=False)
    # _TraceGuard 内部是函数内 import，直接打桩构造即可
    monkeypatch.setattr(runner, '_TraceGuard', lambda *a, **k: _Null(), raising=False)
    calls = []

    async def handler():
        calls.append(1)
        return 'ok'

    session = {'session_uuid': 'ss-t5', 'idea': 'x'}
    assert asyncio.run(runner._run_in_trace(session, '开始', handler)) == 'ok'
    assert len(calls) == 1


class _Null:
    scope = None

    def close(self):
        pass


# --------------------------------------------------------------------------
# 2. 研发线程：沿用会话 run / 自建 run
# --------------------------------------------------------------------------

def test_research_scope_inherits_session_run(trace_db):
    """会话轮建过run → 研发沿用同一条，「点了开始」与「8 轮迭代」是连续的一段"""
    from utils.agent_trace.core import TraceRecorder
    from agents.shared.strategy_trace import strategy_research_scope
    # 先模拟「会话轮」：run 行由它建在同一个库文件里（研发线程接上去要过外键，
    # 真实路径正是这样 —— 会话轮在请求线程建 run，研发线程拿同一个 run_id 续写）
    owner = TraceRecorder(agent_name='strategy_session')
    run_id = owner.start_run({'input': '尾盘买入次日卖出'})
    owner.end_run()
    bind = {'trace_run_id': run_id}
    with strategy_research_scope('SR-1', '尾盘买入次日卖出', bind=bind) as scope:
        assert scope.run_id == run_id
        assert scope.created is False
        # 沿用同一条 run 也必须挂 recorder：否则研发 8 轮的 LLM 调用与迭代打点全丢
        # （实测症状：63 条会话 run 的 steps 恒为 0 ⇒ 月历里「有 run、token 恒 0」）
        scope.record_iter(1, 'gen', {'hypothesis': '尾盘动量'})
    rows = _runs(trace_db)
    assert len(rows) == 1, 'attach 不许另插 runs 行（那条 run 属于会话轮）'
    assert rows[0]['id'] == run_id
    assert [s['step_name'] for s in _steps(trace_db, run_id)] == ['第1轮-gen']


def test_research_scope_creates_run_when_no_session(trace_db):
    """面板直接发起（无会话 run）→ 研发自建 run，照样有 trace"""
    from agents.shared.strategy_trace import strategy_research_scope
    bind = {}
    with strategy_research_scope('SR-2', '尾盘买入次日卖出', bind=bind) as scope:
        assert scope.run_id and scope.created is True
    assert bind.get('trace_run_id') == scope.run_id, '必须回传以便落库'
    rows = _runs(trace_db)
    assert len(rows) == 1 and rows[0]['agent_name'] == 'strategy_research'


def test_iter_hook_is_available_inside_scope(trace_db):
    """StrategyResearchLoop 通过 current_iter_hook() 拿打点回调"""
    from agents.shared.strategy_trace import (current_iter_hook, strategy_research_scope)
    assert current_iter_hook() is None
    with strategy_research_scope('SR-3', bind={}) as scope:
        hook = current_iter_hook()
        assert hook is not None
        hook(1, 'gen', {'hypothesis': 'abc'})
    assert current_iter_hook() is None
    names = [s['step_name'] for s in _steps(trace_db, scope.run_id)]
    assert '第1轮-gen' in names


# --------------------------------------------------------------------------
# 3. 迭代埋点
# --------------------------------------------------------------------------

def test_record_iter_writes_per_round_steps(trace_db):
    """8 轮迭代每轮都要看得见（这正是之前完全缺失的那段）"""
    from agents.shared.strategy_trace import strategy_research_scope
    with strategy_research_scope('SR-4', bind={}) as scope:
        for r in range(1, 9):
            scope.record_iter(r, 'gen', {'hypothesis': f'第{r}轮'})
            scope.record_iter(r, 'decide', {'verdict': 'ok'})
        scope.record_iter(0, 'terminate', {'status': 'done', 'total_iters': 8})
    names = [s['step_name'] for s in _steps(trace_db, scope.run_id)]
    assert len(names) == 17, names
    assert names[0] == '第1轮-gen' and names[-1] == '整体-terminate'
    # 轮号 0 只允许出现在「整体」事件上，不能写出「第0轮」
    assert not any(n.startswith('第0轮') for n in names)


def test_record_iter_without_recorder_is_noop(trace_db):
    """无 recorder 的空壳 scope（trace 组件不可用时的降级路径）→ 静默跳过，不抛。

    真实来源：server/routes/strategy_research.py 的 _research_scope 在 strategy_trace
    导入失败时降级为 nullcontext；以及直接构造的 ResearchScope(run_id, recorder=None)。
    埋点是可观测性，不是业务依赖 —— 这里锁死「没有 recorder 也绝不影响研发」。
    """
    from agents.shared.strategy_trace import ResearchScope
    scope = ResearchScope(run_id='x', recorder=None, created=False)
    scope.record_iter(1, 'gen', {'hypothesis': 'y'})   # 不应抛
    assert not os.path.exists(trace_db), '降级路径不该碰库'


def test_research_loop_maps_progress_to_iter_hook(trace_db):
    """StrategyResearchLoop 的 progress 事件要真的映射到 trace phase"""
    from backtest.strategy_research import StrategyResearchLoop
    from agents.shared.strategy_trace import strategy_research_scope

    seen = []

    class _Gen:
        def generate_initial(self, *a, **k):
            raise AssertionError('不该被调用')

        def mutate(self, *a, **k):
            raise AssertionError('不该被调用')

    loop = StrategyResearchLoop(run_uuid='SR-6', db=object(), generator=_Gen(),
                                runner=object(), budget=0)
    with strategy_research_scope('SR-6', bind={}) as scope:
        loop._trace_event('strategy_iter_start',
                          {'round': 1, 'hypothesis': '收盘价上穿 MA20'})
        loop._trace_event('strategy_run_done',
                          {'status': 'done', 'stop_reason': 'max_rounds', 'total_iters': 8})
        seen = [s['step_name'] for s in _steps(trace_db, scope.run_id)]
    assert seen == ['第1轮-gen', '整体-terminate']


def test_trace_event_failure_never_breaks_progress(trace_db, monkeypatch):
    """progress 回调异常不得反过来影响研发主流程（埋点失败要吞掉）"""
    from backtest.strategy_research import StrategyResearchLoop

    loop = StrategyResearchLoop(run_uuid='SR-7', db=object(), generator=object(),
                                runner=object(), progress_cb=lambda e, d: 1 / 0)
    loop.progress_cb('strategy_iter_start', {'round': 1})   # 不应抛


# --------------------------------------------------------------------------
# 4. 收尾：异常路径必须 end_run
# --------------------------------------------------------------------------

def test_exception_closes_run(trace_db):
    """异常时 run 不能停在 running（会一直转圈 + 下次被「猜最近 run」命中）"""
    from agents.shared.strategy_trace import strategy_research_scope
    with pytest.raises(ValueError):
        with strategy_research_scope('SR-8', bind={}) as scope:
            rid = scope.run_id
            raise ValueError('boom')
    rows = _runs(trace_db)
    assert len(rows) == 1
    assert rows[0]['status'] != 'running', rows[0]
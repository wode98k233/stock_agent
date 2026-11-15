"""策略研究「断点续跑」单测（续跑原 run，不新建）。

背景：8 轮研究跑在第 N 轮时进程重启，内存里的 append-only 上下文
（``StrategyGenerator._messages``）消失，run 永久停在 ``running``。
修复分两段：
1. 启动时把残留 ``running`` 收敛为 ``interrupted``（``mark_interrupted_runs``）；
2. 用户点续跑后从 ``strategy_iter`` **重建同形上下文**，接着跑同一 run 的第 N+1 轮。

覆盖：
- ``restore_context`` 消息序列（system → user → assistant/user 交替 → assistant）
- 坏 JSON / 缺失策略的轮次被跳过，不影响其余轮次
- ``run(resume_iters=[...])`` 从第 N+1 轮开始、不再调 ``generate_initial``
- ``mark_interrupted_runs`` 只动 running
- ``POST /runs/{uuid}/resume`` 的 404 / 409 / 400 / 200

LLM 与回测全部 mock，不外呼；数据库全部用 tmp_path。
"""
import asyncio
import json
import os
import sys

import pytest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: F401

from backtest.strategy_research import (
    STOP_BUDGET,
    GenResult,
    IterRecord,
    StrategyGenerator,
    StrategyResearchLoop,
    mark_interrupted_runs,
)


# ============================================================
# 夹具与替身
# ============================================================


@pytest.fixture()
def sr_db(tmp_path):
    """临时 backtest.db（含 strategy_run / strategy_iter 全表）。"""
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


@pytest.fixture(autouse=True)
def _no_intent_fidelity_llm(monkeypatch):
    """关掉意图保真核对（默认开且会真调 LLM，单测里必须关）。"""
    from config import Config
    monkeypatch.setattr(Config, 'STRATEGY_INTENT_FIDELITY', False, raising=False)


def _ok_strategy(name='测试策略'):
    """一份能过编译的最小策略 JSON（MA 金叉）。"""
    return {
        'meta': {'name': name, 'description': '', 'tags': []},
        'params': {
            'fast': {'value': 5, 'min': 2, 'max': 60, 'step': 1, 'label': '快线', 'type': 'int'},
            'slow': {'value': 20, 'min': 5, 'max': 200, 'step': 1, 'label': '慢线', 'type': 'int'},
        },
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator',
                 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_above',
                 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
            'sell': {'logic': 'AND', 'rules': [
                {'type': 'indicator',
                 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_below',
                 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
        },
        'risk': {},
    }


def _iter(round_no, hypothesis, strategy_json):
    return IterRecord(round=round_no, hypothesis=hypothesis,
                      strategy_json=strategy_json,
                      compile_ok=1 if strategy_json else 0, verdict='keep')


class _FakeStore:
    """内存版落库适配。"""

    def __init__(self, run):
        self.runs = {run['run_uuid']: dict(run)}
        self.iters = []
        self._next_id = 1

    def update_run(self, run_uuid, **kw):
        self.runs[run_uuid].update(kw)

    def get_run(self, run_uuid):
        return self.runs.get(run_uuid)

    def insert_iter(self, run_uuid, record):
        record.id = self._next_id
        self._next_id += 1
        self.iters.append(record)
        return record.id

    def set_iter_verdict(self, iter_id, verdict):
        for it in self.iters:
            if it.id == iter_id:
                it.verdict = verdict


class _FakeGenerator:
    """记录调用参数的生成器替身；restore_context 单独记账。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.restored = None

    def restore_context(self, idea, universe, iters):
        self.calls.append(('restore', len(iters)))
        self.restored = (idea, universe, [it.round for it in iters])

    def generate_initial(self, idea, universe):
        self.calls.append(('initial', idea, universe))
        return self._next()

    def mutate(self, idea, universe, history):
        self.calls.append(('mutate', len(history), [it.round for it in history]))
        return self._next()

    def _next(self):
        r = self.results.pop(0) if self.results else self._last
        self._last = r
        return r

    _last = None


class _FakeRunner:
    def __init__(self, metrics):
        self.metrics = metrics

    def run_both(self, strategy_json, universe, period):
        return self.metrics


def _gen_ok(hypothesis='假设'):
    return GenResult(ok=True,
                     strategy_json=json.dumps(_ok_strategy(), ensure_ascii=False),
                     hypothesis=hypothesis, model='fake-model')


def _metrics(sharpe=1.0, trades=20):
    return ({'cagr': 0.1, 'sharpe': sharpe, 'mdd': 0.2, 'win_rate': 0.5, 'trades': trades},
            {'cagr': 0.08, 'sharpe': sharpe, 'mdd': 0.15, 'win_rate': 0.5, 'trades': trades})


def _new_store(run_uuid='SR-RESUME'):
    return _FakeStore({'run_uuid': run_uuid, 'idea': '测试想法', 'universe': '600519',
                       'period': '2023-01-01~2026-01-01', 'status': 'running'})


# ============================================================
# 1. restore_context：消息序列必须与 append-only 同形
# ============================================================


class TestRestoreContext:
    def test_message_sequence_matches_append_only(self, sr_db):
        gen = StrategyGenerator()
        it1 = _iter(1, '假设一', json.dumps(_ok_strategy('s1'), ensure_ascii=False))
        it2 = _iter(2, '假设二', json.dumps(_ok_strategy('s2'), ensure_ascii=False))
        it3 = _iter(3, '假设三', json.dumps(_ok_strategy('s3'), ensure_ascii=False))

        gen.restore_context('研究想法', '600519', [it1, it2, it3])

        roles = [role for role, _ in gen._messages]
        assert roles[0] == 'system'
        assert roles[1] == 'user'
        assert roles[-1] == 'assistant'
        # [system][user] + (assistant, user) × 2 + assistant
        assert roles[2:] == ['assistant', 'user', 'assistant', 'user', 'assistant']
        assert gen._round_no == 3          # 下一轮 mutate 才是第 4 轮
        assert gen._compressed is False

        # 首轮 user 是初始想法 prompt（不是 round_prompt）
        assert '研究想法：研究想法' in gen._messages[1][1]
        # 每条 user 都是**上一轮**的结果描述，且轮号对得上
        assert '- 第1轮 假设：假设一' in gen._messages[3][1]
        assert '- 第2轮 假设：假设二' in gen._messages[5][1]

        # assistant 还原成模型外层输出形状 {"hypothesis":..., "strategy":...}
        raw = json.loads(gen._messages[2][1])
        assert raw['hypothesis'] == '假设一'
        assert raw['strategy']['meta']['name'] == 's1'

    def test_bad_json_iter_is_skipped(self, sr_db):
        gen = StrategyGenerator()
        it1 = _iter(1, '假设一', json.dumps(_ok_strategy('s1'), ensure_ascii=False))
        bad = _iter(2, '假设二', '{"meta": 这不是 JSON')
        it3 = _iter(3, '假设三', json.dumps(_ok_strategy('s3'), ensure_ascii=False))

        gen.restore_context('研究想法', '600519', [it1, bad, it3])

        roles = [role for role, _ in gen._messages]
        assert roles == ['system', 'user', 'assistant', 'user', 'assistant']
        joined = '\n'.join(content for _, content in gen._messages)
        assert '这不是 JSON' not in joined          # 坏数据没有混进上下文
        # 坏轮被跳过，但后续轮次照常接上
        assert '- 第1轮 假设：假设一' in gen._messages[3][1]
        assert json.loads(gen._messages[4][1])['strategy']['meta']['name'] == 's3'
        assert gen._round_no == 3                   # 轮号仍按库里的真实轮次推进

    def test_missing_strategy_json_is_skipped(self, sr_db):
        """生成失败轮（compile_ok=0，无 strategy_json）不能把续跑搞崩。"""
        gen = StrategyGenerator()
        it1 = _iter(1, '假设一', json.dumps(_ok_strategy('s1'), ensure_ascii=False))
        it2 = _iter(2, '', None)

        gen.restore_context('研究想法', '600519', [it1, it2])

        roles = [role for role, _ in gen._messages]
        assert roles == ['system', 'user', 'assistant']
        assert gen._round_no == 2


# ============================================================
# 2. run(resume_iters=...)：从第 N+1 轮开始，不重跑首轮
# ============================================================


class TestRunResume:
    def test_resume_starts_at_next_round_without_initial(self, sr_db):
        store = _new_store()
        it1 = _iter(1, '假设一', json.dumps(_ok_strategy('s1'), ensure_ascii=False))
        it2 = _iter(2, '假设二', json.dumps(_ok_strategy('s2'), ensure_ascii=False))
        gen = _FakeGenerator([_gen_ok('第三轮')])
        is_m, oos_m = _metrics()

        loop = StrategyResearchLoop('SR-RESUME', db=store, generator=gen,
                                    runner=_FakeRunner((is_m, oos_m)),
                                    budget=1)   # 跑 1 轮后 budget 终止，聚焦「起点」
        result = loop.run(resume_iters=[it1, it2])

        assert gen.calls[0] == ('restore', 2)
        assert gen.restored[2] == [1, 2]                 # 用库里真实轮次重建
        assert 'initial' not in [c[0] for c in gen.calls]  # 首轮不重跑
        # 第一次 mutate 拿到的历史就是续跑的 2 条
        assert gen.calls[1] == ('mutate', 2, [1, 2])
        # 新落库的轮次从第 3 轮开始
        assert [it.round for it in store.iters] == [3]
        assert result.stop_reason == STOP_BUDGET
        assert result.status == 'done'

    def test_normal_run_still_uses_generate_initial(self, sr_db):
        """回归：不传 resume_iters 时行为完全不变。"""
        store = _new_store()
        gen = _FakeGenerator([_gen_ok('第一轮')])
        is_m, oos_m = _metrics()
        loop = StrategyResearchLoop('SR-RESUME', db=store, generator=gen,
                                    runner=_FakeRunner((is_m, oos_m)), budget=1)
        loop.run()

        assert gen.calls[0][0] == 'initial'
        assert [it.round for it in store.iters] == [1]


# ============================================================
# 3. mark_interrupted_runs：只收敛 running
# ============================================================


class TestMarkInterrupted:
    def test_only_running_becomes_interrupted(self, sr_db):
        from utils.cache.backtest_db import (
            create_strategy_run, get_strategy_run, update_strategy_run,
        )
        for uuid in ('SR-RUN-1', 'SR-RUN-2'):
            create_strategy_run(uuid, '想法', '600519', 'a~b')
        create_strategy_run('SR-DONE', '想法', '600519', 'a~b')
        update_strategy_run('SR-DONE', status='done', stop_reason='converged')

        count = mark_interrupted_runs()

        assert count == 2
        for uuid in ('SR-RUN-1', 'SR-RUN-2'):
            run = get_strategy_run(uuid)
            assert run['status'] == 'interrupted'
            assert run['stop_reason'] == '服务重启，任务中断'
        assert get_strategy_run('SR-DONE')['status'] == 'done'

    def test_second_call_is_noop(self, sr_db):
        from utils.cache.backtest_db import create_strategy_run
        create_strategy_run('SR-ONCE', '想法', '600519', 'a~b')
        assert mark_interrupted_runs() == 1
        assert mark_interrupted_runs() == 0


# ============================================================
# 4. POST /runs/{uuid}/resume
# ============================================================


def _call_resume(run_uuid):
    from server.routes.strategy_research import resume_run

    async def _run():
        return await resume_run(run_uuid)

    return asyncio.run(_run())


@pytest.fixture()
def _no_thread(monkeypatch):
    """不许真的起研发线程（否则会去外呼 LLM / 读真实行情）。"""
    import server.routes.strategy_research as sr_mod
    calls = []
    monkeypatch.setattr(sr_mod, '_run_loop_async',
                        lambda *a, **kw: calls.append(a))
    return calls


class TestResumeApi:
    def test_unknown_run_404(self, sr_db):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            _call_resume('SR-NOPE')
        assert ei.value.status_code == 404

    def test_not_interrupted_409(self, sr_db):
        from fastapi import HTTPException
        from utils.cache.backtest_db import create_strategy_run
        create_strategy_run('SR-RUNNING', '想法', '600519', 'a~b')   # 默认 running
        with pytest.raises(HTTPException) as ei:
            _call_resume('SR-RUNNING')
        assert ei.value.status_code == 409
        assert '只有已中断的任务可以续跑' in str(ei.value.detail)
        assert 'status=running' in str(ei.value.detail)

    def test_no_iters_400(self, sr_db):
        from fastapi import HTTPException
        from utils.cache.backtest_db import create_strategy_run, update_strategy_run
        create_strategy_run('SR-EMPTY', '想法', '600519', 'a~b')
        update_strategy_run('SR-EMPTY', status='interrupted')
        with pytest.raises(HTTPException) as ei:
            _call_resume('SR-EMPTY')
        assert ei.value.status_code == 400
        assert '没有可续跑的轮次' in str(ei.value.detail)

    def test_list_runs_attaches_iter_count_for_interrupted(self, sr_db):
        """历史项要显示「从第 N 轮续跑」，N 只能由 iter_count 给（total_iters 是 0）。"""
        from utils.cache.backtest_db import (
            create_strategy_run, insert_strategy_iter, update_strategy_run,
        )
        from server.routes.strategy_research import list_runs

        create_strategy_run('SR-LIST', '想法', '600519', 'a~b')
        for round_no in (1, 2, 3):
            insert_strategy_iter(
                'SR-LIST', round_no=round_no, hypothesis='h', strategy_json='{}',
                strategy_hash=f'h{round_no}', strategy_diff='x', compile_ok=1,
                compile_error=None, is_metrics={'sharpe': 1.0},
                oos_metrics={'sharpe': 0.9, 'trades': 20}, verdict='keep')
        update_strategy_run('SR-LIST', status='interrupted')

        res = asyncio.run(list_runs(limit=50))
        row = next(r for r in res['data']['runs'] if r['run_uuid'] == 'SR-LIST')
        assert row['iter_count'] == 3

    def test_resume_sets_running_and_starts_thread(self, sr_db, _no_thread):
        from utils.cache.backtest_db import (
            create_strategy_run, get_strategy_run, insert_strategy_iter,
            update_strategy_run,
        )
        create_strategy_run('SR-OK', '想法', '600519', '2023-01-01~2026-01-01')
        for round_no in (1, 2):
            insert_strategy_iter(
                'SR-OK', round_no=round_no, hypothesis=f'假设{round_no}',
                strategy_json=json.dumps(_ok_strategy(), ensure_ascii=False),
                strategy_hash=f'h{round_no}', strategy_diff='首轮生成',
                compile_ok=1, compile_error=None,
                is_metrics={'sharpe': 1.0}, oos_metrics={'sharpe': 0.9, 'trades': 20},
                overfit_flag=0, verdict='keep', model='m')
        update_strategy_run('SR-OK', status='interrupted', stop_reason='服务重启，任务中断')

        res = _call_resume('SR-OK')

        assert res['code'] == 0
        assert res['data']['run_uuid'] == 'SR-OK'
        assert res['data']['events_url'].endswith('/runs/SR-OK/events')
        assert res['data']['resume_from_round'] == 3
        assert get_strategy_run('SR-OK')['status'] == 'running'
        assert get_strategy_run('SR-OK')['stop_reason'] == ''
        # 线程已按「续跑 2 条轮次」起跑
        assert len(_no_thread) == 1
        assert _no_thread[0][0] == 'SR-OK'
        assert [it.round for it in _no_thread[0][4]] == [1, 2]

"""A3 服务层 adopt 三动作单测（AC-9 固化）+ 敏感词接口层校验 + SSE 回归

- adopt=track：写 signal_eval.signal（合成 message_uuid='sr_'+run_uuid，confidence=NULL），重复幂等
- adopt=save：最优策略回写策略库（临时 backtest.db）
- adopt=monitor：对接 MonitorStore 规则创建（临时 monitor_alerts.db）
- start_strategy_run：敏感词想法拒绝（AC-8 接口侧）
- SSE events：running 任务轮询不抛 NameError（回归：曾漏 import asyncio）、
  空队列正常等待后超时 task_error、队列事件逐条下发且遇 final 关流

全部用临时库，不碰真实数据。
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


@pytest.fixture()
def isolated_dbs(tmp_path):
    """临时 backtest.db + signal_eval.db + monitor_alerts.db。"""
    bt_db = str(tmp_path / 'backtest.db')
    se_db = str(tmp_path / 'signal_eval.db')
    mo_db = str(tmp_path / 'monitor_alerts.db')

    with patch('config.Config.get_backtest_db_path', return_value=bt_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        # signal_eval：替换库路径并重置线程本地连接
        import signal_eval.db as se_db_mod
        with patch.object(se_db_mod, 'SIGNAL_EVAL_DB_PATH', se_db):
            se_db_mod.close_conn()
            se_db_mod.init_db(se_db)
            # monitor：替换库路径
            with patch('server.monitoring.store.get_monitor_alerts_db_path',
                       return_value=mo_db):
                yield bt_db


@pytest.fixture()
def done_run(isolated_dbs):
    """构造一个 status=done 且有最优轮次的任务（合法可编译策略 JSON）。"""
    from utils.cache.backtest_db import (
        create_strategy_run, insert_strategy_iter, update_strategy_run,
    )
    strategy = {
        'meta': {'name': '测试策略', 'description': 'MA 金叉', 'tags': []},
        'params': {'fast': {'value': 5, 'min': 2, 'max': 60, 'step': 1, 'label': '快线', 'type': 'int'},
                   'slow': {'value': 20, 'min': 5, 'max': 200, 'step': 1, 'label': '慢线', 'type': 'int'}},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_above', 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
            'sell': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_below', 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
        },
        'risk': {'stop_loss': {'type': 'fixed', 'value': 0.08}},
    }
    create_strategy_run('SR-ADOPT', '测试想法', '600519', '2023-01-01~2026-01-01',
                        source='panel', dialog_uuid='dlg-x')
    it_id = insert_strategy_iter('SR-ADOPT', round_no=1, hypothesis='假设',
                                 strategy_json=json.dumps(strategy, ensure_ascii=False),
                                 strategy_hash='h', strategy_diff='首轮生成',
                                 compile_ok=1, compile_error=None,
                                 is_metrics={'sharpe': 1.0},
                                 oos_metrics={'sharpe': 0.9, 'trades': 10},
                                 overfit_flag=0, verdict='keep', model='m')
    update_strategy_run('SR-ADOPT', status='done', stop_reason='converged',
                        best_iter_id=it_id, total_iters=1)
    return 'SR-ADOPT'


class TestAdoptTrack:
    def test_track_insert_and_idempotent(self, isolated_dbs, done_run):
        """AC-9：track 写入 signal 表且可查；重复 adopt 幂等。"""
        from utils.cache.backtest_db import get_strategy_run
        from server.routes.strategy_research import _adopt_track
        from signal_eval.db import get_conn

        run = get_strategy_run(done_run)
        from utils.cache.backtest_db import get_strategy_iter_by_id
        best = get_strategy_iter_by_id(run['best_iter_id'])

        r1 = _adopt_track(run, best)
        assert r1['code'] == 0
        assert r1['data']['message_uuid'] == 'sr_SR-ADOPT'
        assert r1['data']['symbol_key'] == 'sh600519'

        r2 = _adopt_track(run, best)
        assert r2['code'] == 0
        assert r2['data'].get('already') is True

        row = get_conn().execute(
            "SELECT * FROM signal WHERE message_uuid = 'sr_SR-ADOPT'").fetchone()
        assert row is not None
        rec = dict(row)
        assert rec['decision'] == 'buy'
        assert rec['confidence'] is None          # 不污染 A2 校准样本
        assert rec['symbol_key'] == 'sh600519'
        assert rec['core_verdict'].startswith('策略研究:')
        assert rec['dialog_uuid'] == 'dlg-x'


class TestAdoptSave:
    def test_save_writes_strategy_library(self, isolated_dbs, done_run):
        from utils.cache.backtest_db import get_strategy_run, get_strategy_iter_by_id
        from server.routes.strategy_research import _adopt_save

        run = get_strategy_run(done_run)
        best = get_strategy_iter_by_id(run['best_iter_id'])
        res = _adopt_save(run, best)
        assert res['code'] == 0
        strategy_id = res['data']['strategy_id']
        assert strategy_id.startswith('sr_')

        from utils.cache.backtest_db import get_strategy
        saved = get_strategy(strategy_id)
        assert saved is not None
        code = json.loads(saved['code'])
        assert code['meta']['name'] == '测试策略'


class TestAdoptMonitor:
    def test_monitor_creates_rule(self, isolated_dbs, done_run):
        from utils.cache.backtest_db import get_strategy_run, get_strategy_iter_by_id
        from server.routes.strategy_research import _adopt_monitor

        run = get_strategy_run(done_run)
        best = get_strategy_iter_by_id(run['best_iter_id'])

        # 本地无最新价 → code 4210（不 500，M6 降级语义）
        res = _adopt_monitor(run, best)
        if res['code'] != 0:
            assert res['code'] == 4210
            return
        # 本地有价：规则真实创建，类型为 price_below，阈值 = 最新价 × (1-0.08)
        assert res['code'] == 0
        assert res['data']['rule_type'] == 'price_below'
        from server.monitoring.store import MonitorStore
        rules = MonitorStore().list_rules()
        assert any(r['id'] == res['data']['rule_id'] for r in rules)
        assert rules[0]['source'] == 'strategy_research'


class TestStartRunValidation:
    def test_sensitive_idea_rejected(self, isolated_dbs):
        """AC-8 接口侧：含敏感词的想法被拒绝。"""
        from server.routes.strategy_research import start_strategy_run
        with pytest.raises(ValueError, match='不合规词汇'):
            start_strategy_run('帮我找个稳赚的策略', '600519')

    def test_bad_universe_rejected(self, isolated_dbs):
        from server.routes.strategy_research import start_strategy_run
        with pytest.raises(ValueError, match='6 位'):
            start_strategy_run('一个正常的想法描述', 'AAPL')

    def test_short_idea_rejected(self, isolated_dbs):
        from server.routes.strategy_research import start_strategy_run
        with pytest.raises(ValueError, match='过短'):
            start_strategy_run('abc', '600519')


# ============================================================
# SSE events 端点回归（QA 对抗脚本 A3-g 节：漏 import asyncio 修复固化）
# ============================================================


@pytest.fixture()
def running_run(isolated_dbs):
    """构造一个 status=running 的任务（SSE 轮询路径前置）。"""
    from utils.cache.backtest_db import create_strategy_run
    create_strategy_run('SR-SSE', 'SSE 回归测试想法', '600519',
                        '2023-01-01~2026-01-01', source='panel',
                        dialog_uuid='dlg-sse')
    return 'SR-SSE'


def _collect_sse(run_uuid: str) -> list:
    """直连 stream_events 端点函数，收集全部 SSE 帧文本。"""
    from server.routes.strategy_research import stream_events

    async def _collect() -> list:
        resp = await stream_events(run_uuid)
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode('utf-8'))
        return out

    return asyncio.run(_collect())


@pytest.fixture()
def fast_sse():
    """把 SSE 轮询常量调小，测试秒级完成（模块级常量运行时读取，patch 生效）。"""
    import server.routes.strategy_research as sr_mod
    with patch.object(sr_mod, 'SSE_MAX_TICKS', 1), \
         patch.object(sr_mod, 'SSE_POLL_SECONDS', 0.01):
        yield sr_mod


@pytest.fixture(autouse=True)
def _clean_sr_events():
    """每个用例前后清空进程内 SSE 事件队列，避免跨用例污染。"""
    import server.routes.strategy_research as sr_mod
    sr_mod._sr_events.clear()
    yield
    sr_mod._sr_events.clear()


class TestSseEvents:
    def test_running_task_events_no_nameerror(self, running_run, fast_sse):
        """回归：running 任务（空队列）下 events 端点完整走通轮询路径。

        修复前：await asyncio.sleep 处 NameError（模块漏 import asyncio）
        导致 SSE 流中断；修复后空队列正常等待，SSE_MAX_TICKS=1 时
        以 task_error「SSE 连接超时」帧收尾关流。
        """
        chunks = _collect_sse(running_run)
        assert ''.join(chunks), 'SSE 流不应为空'
        text = ''.join(chunks)
        assert 'event: task_error' in text
        assert 'SSE 连接超时' in text

    def test_events_delivered_then_final_closes(self, running_run, fast_sse):
        """队列事件逐条下发，遇 final 帧立即关流（客户端重连场景兜底）。"""
        import server.routes.strategy_research as sr_mod
        sr_mod._push_event(running_run, 'strategy_iter_start',
                           {'run_uuid': running_run, 'round': 1})
        sr_mod._push_event(running_run, 'strategy_iter_done',
                           {'run_uuid': running_run, 'round': 1, 'verdict': 'keep'})
        sr_mod._push_event(running_run, 'final',
                           {'run_uuid': running_run, 'status': 'done'})

        text = ''.join(_collect_sse(running_run))
        assert 'event: strategy_iter_start' in text
        assert 'event: strategy_iter_done' in text
        assert 'event: final' in text

    def test_terminal_status_emits_db_fallback_final(self, isolated_dbs):
        """终态任务连 SSE：直接补发 final（db_fallback），不再进入轮询。"""
        from utils.cache.backtest_db import create_strategy_run, update_strategy_run
        create_strategy_run('SR-SSE-DONE', '终态直连测试想法', '600519',
                            '2023-01-01~2026-01-01', source='panel',
                            dialog_uuid='dlg-sse-done')
        update_strategy_run('SR-SSE-DONE', status='done', stop_reason='converged')
        text = ''.join(_collect_sse('SR-SSE-DONE'))
        assert 'event: final' in text
        assert 'db_fallback' in text
        assert 'converged' in text

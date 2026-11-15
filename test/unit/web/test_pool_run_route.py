"""池子优先入口 /api/strategy-research/pool-run 的最小契约测试。"""
import asyncio
import json

import server.routes.strategy_research as sr


def test_pool_run_returns_result(monkeypatch):
    monkeypatch.setattr(
        'backtest.pool_first.run_pool_first',
        lambda *a, **k: {'cross_section': {'overall': {'total': 1}}, 'portfolio': {'total_return': 0.1}},
    )
    req = sr.PoolRunRequest(strategy_json='{"meta": {}}', pool='POOL:600519',
                            start='2024-01-01', end='2024-12-31')
    out = asyncio.run(sr.create_pool_run(req))
    assert out['code'] == 0
    assert out['data']['portfolio']['total_return'] == 0.1


def test_pool_run_rejects_bad_pool():
    req = sr.PoolRunRequest(strategy_json='{"meta": {}}', pool='POOL:abc')
    out = asyncio.run(sr.create_pool_run(req))
    assert out['code'] == 1
    assert '池子' in out['msg']


def test_pool_run_requires_idea_or_strategy():
    req = sr.PoolRunRequest(pool='POOL:600519')
    out = asyncio.run(sr.create_pool_run(req))
    assert out['code'] == 1
    assert 'strategy_json' in out['msg']


def test_start_pool_run_returns_run_id(monkeypatch):
    monkeypatch.setattr(sr, '_run_pool_async', lambda *a, **k: None)
    req = sr.PoolRunRequest(strategy_json='{"meta": {}}', pool='POOL:600519')
    out = asyncio.run(sr.start_pool_run(req))
    assert out['code'] == 0
    run_id = out['data']['run_id']
    assert run_id.startswith('PR-')
    snap = asyncio.run(sr.get_pool_run(run_id))
    assert snap['data']['status'] == 'running'
    sr._pool_runs.pop(run_id, None)


def test_pool_sse_done_sends_final():
    sr._pool_runs['PR-TEST'] = {'status': 'done', 'result': {'n': 1}}
    resp = asyncio.run(sr.stream_pool_events('PR-TEST'))

    async def collect():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return ''.join(chunks)

    text = asyncio.run(collect())
    assert 'event: final' in text
    assert json.dumps({'n': 1}) in text
    sr._pool_runs.pop('PR-TEST', None)


def test_pool_run_report_maps_to_study_shape():
    """对话池子结果要能翻译成「池子研究」页读的报告形状（不能再有页面看不到）。"""
    result = {
        'pool': {'n': 2, 'assets': {'stock': 1, 'etf': 1}},
        'cross_section': {
            'overall': {'total': 2, 'scored': 2, 'valid': 1},
            'verdict': {'key': 'insufficient', 'label': '整体不成立', 'headline': 'h'},
            'results': [
                {'code': '600519', 'health': 'ok', 'trades': 6,
                 'win_rate': 0.6, 'total_return': 0.1},
                {'code': '510300', 'health': 'ok', 'trades': 6,
                 'win_rate': 0.4, 'total_return': -0.1},
            ],
            'caveats': ['边界'],
        },
    }
    rep = sr._pool_run_study_report(result, pool_spec='POOL:default',
                                    start='2024-01-01', end='2024-12-31')
    assert rep['period']['days'] == 365
    assert rep['universe']['total'] == 2
    assert rep['verdict']['label'] == '整体不成立'
    assert rep['overall']['scored'] == 2
    assert [i['bucket'] for i in rep['items']] == ['valid', 'weak']
    assert rep['items'][0]['code'] == '600519'
    assert rep['caveats'] == ['边界']


def test_run_pool_async_persists_done(monkeypatch):
    """跑完必须落盘，否则重启即丢、页面看不到。"""
    import backtest.pool_first as pf

    monkeypatch.setattr(pf, 'resolve_pool', lambda spec: ['600519'])
    result = {'cross_section': {'overall': {'total': 1}, 'verdict': {}, 'results': []},
              'pool': {'n': 1, 'assets': {'stock': 1, 'etf': 0}}}
    monkeypatch.setattr(pf, 'run_pool_first', lambda *a, **k: result)
    saved = {}
    monkeypatch.setattr(sr, '_persist_pool_run',
                        lambda run_id, **kw: saved.update({'run_id': run_id, **kw}))
    req = sr.PoolRunRequest(strategy_json='{"meta":{}}', pool='POOL:600519',
                            start='2024-01-01', end='2024-12-31')
    sr._run_pool_async('PR-T', req)
    assert saved['run_id'] == 'PR-T' and saved['result'] == result
    assert saved['elapsed_sec'] >= 0
    sr._pool_runs.pop('PR-T', None)


def test_run_pool_async_passes_sweep_context_to_generation(monkeypatch):
    """生成阶段要把「持有期矩阵 / 卖出口径对比」告诉意图核对器，避免误报偏离。"""
    import backtest.pool_first as pf

    monkeypatch.setattr(pf, 'resolve_pool', lambda spec: ['600519'])
    seen = {}

    def fake_gen(idea, desc, **kw):
        seen.update(kw)
        return {'ok': True, 'strategy_json': '{"conditions": {}}',
                'hypothesis': 'h', 'intent_check': {}}

    result = {'cross_section': {'overall': {'total': 1}, 'verdict': {}, 'results': []},
              'pool': {'n': 1, 'assets': {}}}
    monkeypatch.setattr(pf, 'generate_pool_strategy', fake_gen)
    monkeypatch.setattr(pf, 'run_pool_first', lambda *a, **k: result)
    monkeypatch.setattr(sr, '_persist_pool_run', lambda *a, **k: None)
    req = sr.PoolRunRequest(idea='深V反弹买', pool='POOL:600519',
                            start='2024-01-01', end='2024-12-31',
                            hold_periods=[3, 4, 5], compare_exit_timings=True)
    sr._run_pool_async('PR-GEN', req)
    assert seen['sweep_holds'] == [3, 4, 5]
    assert seen['compare_exit'] is True
    sr._pool_runs.pop('PR-GEN', None)


def test_run_pool_async_persists_failure(monkeypatch):
    """失败也要落记录（页面要能看到失败，不是静默消失）。"""
    import backtest.pool_first as pf

    def boom(spec):
        raise RuntimeError('boom')

    monkeypatch.setattr(pf, 'resolve_pool', boom)
    saved = {}
    monkeypatch.setattr(sr, '_persist_pool_run',
                        lambda run_id, **kw: saved.update({'run_id': run_id, **kw}))
    req = sr.PoolRunRequest(strategy_json='{"meta":{}}', pool='POOL:600519')
    sr._run_pool_async('PR-T2', req)
    assert saved['run_id'] == 'PR-T2' and saved.get('result') is None
    assert 'boom' in saved['error']
    sr._pool_runs.pop('PR-T2', None)


def test_run_pool_async_compare_runs_both_exit_timings(monkeypatch):
    """对比模式：同一策略按两种卖出口径各跑一遍，结果里带对比摘要。"""
    import backtest.pool_first as pf

    monkeypatch.setattr(pf, 'resolve_pool', lambda spec: ['600519'])
    calls = []

    def fake_run(strategy_json, **kw):
        mode = kw.get('exit_timing')
        calls.append(mode)
        return {'cross_section': {'overall': {'total': 1}, 'verdict': {'label': 'x'},
                                  'results': []},
                'pool': {'n': 1, 'assets': {'stock': 1, 'etf': 0}},
                'portfolio': {'total_return': 0.1 if mode == 'next_open' else 0.2},
                'timing': {'entry': 'next_open', 'exit': mode}}

    monkeypatch.setattr(pf, 'run_pool_first', fake_run)
    saved = {}
    monkeypatch.setattr(sr, '_persist_pool_run', lambda run_id, **kw: saved.update(kw))
    req = sr.PoolRunRequest(strategy_json='{"meta":{}}', pool='POOL:600519',
                            start='2024-01-01', end='2024-12-31',
                            entry_timing='next_open', exit_timing='next_open',
                            compare_exit_timings=True)
    sr._run_pool_async('PR-CMP', req)
    assert calls == ['next_open', 'next_close']
    res = saved['result']
    assert res['compare']['modes'] == ['next_open', 'next_close']
    assert res['compare']['rows']['next_close']['portfolio']['total_return'] == 0.2
    sr._pool_runs.pop('PR-CMP', None)


def test_run_pool_async_hold_sweep(monkeypatch):
    """持有期 × 卖出口径矩阵：只改 hold_bars 与口径，其余不变。"""
    import backtest.pool_first as pf

    monkeypatch.setattr(pf, 'resolve_pool', lambda spec: ['600519'])
    calls = []
    progress = []

    def fake_run(spec_json, **kw):
        d = json.loads(spec_json)
        hold = d['conditions']['sell']['hold_bars']
        mode = kw.get('exit_timing')
        calls.append((hold, mode))
        kw['progress_cb']('pool_progress', {'done': 1, 'total': 1, 'ok': 1, 'fail': 0})
        progress.append(sr._pool_runs.get('PR-SW', {}).get('progress') or {})
        return {'cross_section': {'overall': {'total': 1}, 'verdict': {}, 'results': []},
                'pool': {'n': 1, 'assets': {}},
                'portfolio': {'total_return': hold / 100.0 if mode == 'next_open'
                              else hold / 50.0},
                'timing': {'entry': 'next_open', 'exit': mode}}

    monkeypatch.setattr(pf, 'run_pool_first', fake_run)
    saved = {}
    monkeypatch.setattr(sr, '_persist_pool_run', lambda run_id, **kw: saved.update(kw))
    req = sr.PoolRunRequest(
        strategy_json=json.dumps({'conditions': {'sell': {'hold_bars': 3}}}),
        pool='POOL:600519', start='2024-01-01', end='2024-12-31',
        entry_timing='next_open', exit_timing='next_open',
        compare_exit_timings=True, hold_periods=[3, 4])
    sr._run_pool_async('PR-SW', req)
    assert set(calls) == {(3, 'next_open'), (3, 'next_close'),
                          (4, 'next_open'), (4, 'next_close')}
    assert len(calls) == 4
    # 进度条要跨组累计（round/rounds），否则每组都从 0 重来，看起来像卡住
    assert [p.get('round') for p in progress] == [1, 2, 3, 4]
    assert {p.get('rounds') for p in progress} == {4}
    sw = saved['result']['sweep']
    assert sw['holds'] == [3, 4] and sw['modes'] == ['next_open', 'next_close']
    assert sw['rows']['4_next_close']['portfolio']['total_return'] == 4 / 50.0
    sr._pool_runs.pop('PR-SW', None)


def test_pool_run_report_keeps_compare():
    result = {'cross_section': {'overall': {'total': 1}, 'verdict': {}, 'results': []},
              'pool': {'n': 1, 'assets': {}},
              'compare': {'primary': 'next_open',
                          'modes': ['next_open', 'next_close'], 'rows': {}}}
    rep = sr._pool_run_study_report(result, pool_spec='POOL:default',
                                    start='2024-01-01', end='2024-12-31')
    assert rep['compare']['modes'] == ['next_open', 'next_close']


def test_pool_run_report_items_keep_quality_metrics():
    """逐只明细要带夏普/盈亏比/平均持有/回撤，页面才能显示单票研究同款维度。"""
    result = {
        'cross_section': {
            'results': [{'code': '600519', 'name': '贵州茅台', 'health': 'ok',
                         'trades': 6, 'win_rate': 0.6, 'total_return': 0.1,
                         'sharpe': 1.2, 'profit_factor': 2.0,
                         'avg_hold_days': 4.0, 'max_drawdown': -0.1}],
            'overall': {}, 'verdict': {},
        },
        'pool': {'n': 1, 'assets': {}},
    }
    rep = sr._pool_run_study_report(result, pool_spec='POOL:600519',
                                    start='2024-01-01', end='2024-12-31')
    it = rep['items'][0]
    assert it['sharpe'] == 1.2 and it['profit_factor'] == 2.0
    assert it['avg_hold_days'] == 4.0 and it['max_drawdown'] == -0.1

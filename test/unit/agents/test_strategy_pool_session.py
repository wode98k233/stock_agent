"""池子优先会话分支：启动异步任务 / 轮询结果，不查 strategy_run。"""
import asyncio
import json

import agents.shared.strategy_session_runner as ssr


RESULT = {
    'pool': {'n': 40, 'assets': {'stock': 25, 'etf': 15}},
    'cross_section': {
        'overall': {'scored': 24, 'valid': 7, 'valid_ratio': 0.2917,
                    'win_rate_median': 0.3961, 'total_return_median': 0.1026},
        'verdict': {'label': '整体不成立', 'headline': '有效占比不足'},
        'caveats': ['默认池不是全随机'],
    },
    'portfolio': {'initial_cash': 1_000_000, 'final_equity': 1_079_776,
                  'total_return': 0.0798, 'cagr': 0.0278, 'max_drawdown': -0.111,
                  'buys': 371, 'sells': 371, 'win_rate': 0.3477, 'max_positions': 10},
}


def test_is_pool_universe():
    assert ssr._is_pool_universe({'universe': 'POOL:default'})
    assert not ssr._is_pool_universe({'universe': '600519'})


def test_pool_result_text_has_both_layers():
    text = ssr._pool_result_text(RESULT)
    assert '池子验证完成' in text
    assert '整体不成立' in text
    assert '组合' in text and '1,079,776' in text


def test_pool_result_text_renders_timing_compare():
    """对比模式要在结论里给出两种卖法的并排表 + 差值。"""
    result = dict(RESULT)
    result['compare'] = {
        'primary': 'next_open',
        'modes': ['next_open', 'next_close'],
        'rows': {
            'next_open': {'label': '次日开盘价',
                          'overall': {'valid_ratio': 0.3, 'win_rate_median': 0.5,
                                      'total_return_median': 0.01},
                          'portfolio': {'total_return': 0.1, 'max_drawdown': -0.2}},
            'next_close': {'label': '次日收盘价',
                           'overall': {'valid_ratio': 0.4, 'win_rate_median': 0.55,
                                       'total_return_median': 0.02},
                           'portfolio': {'total_return': 0.15, 'max_drawdown': -0.18}},
        },
    }
    text = ssr._pool_result_text(result)
    assert '卖出口径对比' in text
    assert '次日开盘价' in text and '次日收盘价' in text
    assert '差值' in text
    assert '主口径' in text


def test_pool_result_text_renders_sweep_matrix():
    """持有期 × 卖出口径矩阵：每行一组，末尾给收益/胜率最高的组合。"""
    result = dict(RESULT)
    rows = {}
    for hold in (3, 4):
        for mode, ret in (('next_open', 0.10), ('next_close', 0.15)):
            rows[f'{hold}_{mode}'] = {
                'hold': hold, 'timing': mode,
                'label': f'持有 {hold} 根 · ' + ('次日开盘价' if mode == 'next_open' else '次日收盘价'),
                'overall': {'win_rate_median': 0.5 if hold == 3 else 0.6,
                            'total_return_median': ret},
                'portfolio': {'total_return': ret + hold / 100.0, 'max_drawdown': -0.2},
            }
    result['sweep'] = {'holds': [3, 4], 'modes': ['next_open', 'next_close'],
                       'primary': [3, 'next_open'], 'rows': rows}
    text = ssr._pool_result_text(result)
    assert '持有期 × 卖出口径矩阵' in text
    assert '持有 3 根' in text and '持有 4 根' in text
    assert '组合收益最高' in text and '胜率中位最高' in text
    assert '主组合' in text


def test_start_and_report_starts_async_without_strategy_run(monkeypatch):
    async def fake_pool(session):
        session['pool_run_id'] = 'PR-T'
        return 'PR-T'

    async def fail_start(*a, **k):
        raise AssertionError('单票入口不应被调用')

    monkeypatch.setattr(ssr, '_start_pool_async', fake_pool)
    monkeypatch.setattr(ssr, '_start_run', fail_start)
    out = asyncio.run(ssr._start_and_report({'universe': 'POOL:default'}, 'd', None))
    assert '已开跑' in out and 'PR-T' in out


def test_pool_active_returns_cached_result():
    out = asyncio.run(ssr._handle_pool_active(
        {'universe': 'POOL:default', 'pool_result': RESULT}, '跑到哪了', 'd', None,
        {'intent': 'progress'}))
    assert '整体不成立' in out


def test_pool_active_polls_done(monkeypatch):
    import server.routes.strategy_research as sr

    async def fake_get(run_id):
        return {'code': 0, 'data': {'status': 'done', 'result': RESULT}}

    monkeypatch.setattr(sr, 'get_pool_run', fake_get)
    session = {'universe': 'POOL:default', 'pool_run_id': 'PR-T'}
    out = asyncio.run(ssr._handle_pool_active(session, '跑到哪了', 'd', None,
                                              {'intent': 'progress'}))
    assert '整体不成立' in out
    assert session['pool_result'] is RESULT


def test_pool_progress_uses_persisted_run_uuid(monkeypatch):
    '''跨轮重载后 pool_run_id 没了，必须用落库的 run_uuid 续查进度。

    真机事故：问一句「跑到哪了」就新开一条池子回测（旧任务还在后台跑），
    结论永远拿不到。根因是 pool_run_id 只在内存、会话每轮从 DB 重载。
    '''
    import server.routes.strategy_research as sr

    async def fake_get(run_id):
        return {'code': 0, 'data': {'status': 'running',
                                    'progress': {'done': 3, 'total': 40, 'ok': 3, 'fail': 0}}}

    async def no_start(session):
        raise AssertionError('问进度不许再起一条池子回测')

    monkeypatch.setattr(sr, 'get_pool_run', fake_get)
    monkeypatch.setattr(ssr, '_start_pool_async', no_start)
    session = {'universe': 'POOL:default', 'run_uuid': 'PR-T'}
    out = asyncio.run(ssr._handle_pool_active(session, '跑到哪了', 'd', None,
                                              {'intent': 'progress'}))
    assert '还在跑' in out and '3/40' in out


def test_pool_lost_run_does_not_restart(monkeypatch):
    '''任务丢了（服务重启）要明说，绝不能偷偷重开一条全池回测。'''
    import server.routes.strategy_research as sr

    async def lost(run_id):
        raise RuntimeError('404: 池子任务不存在')

    async def no_start(session):
        raise AssertionError('任务丢了不许偷偷重开')

    monkeypatch.setattr(sr, 'get_pool_run', lost)
    monkeypatch.setattr(ssr, '_start_pool_async', no_start)
    session = {'universe': 'POOL:default', 'run_uuid': 'PR-T'}
    out = asyncio.run(ssr._handle_pool_active(session, '跑到哪了', 'd', None,
                                              {'intent': 'progress'}))
    assert '丢了' in out


def test_pool_done_keeps_session_open(monkeypatch):
    '''结果给完仍保持 active：置 done 会让会话从 open 查询里消失，后续改条件/重跑全失效。'''
    import server.routes.strategy_research as sr

    async def fake_get(run_id):
        return {'code': 0, 'data': {'status': 'done', 'result': RESULT}}

    monkeypatch.setattr(sr, 'get_pool_run', fake_get)
    writes = {}
    monkeypatch.setattr(ssr, '_update_session', lambda s, **kw: writes.update(kw))
    session = {'universe': 'POOL:default', 'run_uuid': 'PR-T'}
    out = asyncio.run(ssr._handle_pool_active(session, '跑到哪了', 'd', None,
                                              {'intent': 'progress'}))
    assert '整体不成立' in out
    assert writes.get('status') == 'active'


def test_pool_amend_patches_period_and_shows_note():
    '''池子会话改区间同样要写回槽位并进卡（起跑参数直接取 period_start 列）。'''
    session = {'universe': 'POOL:default', 'status': 'active', 'run_uuid': 'PR-T',
               'slots_json': json.dumps({
                   'universe': {'value': 'POOL:default', 'source': 'default_pool'},
                   'period': {'value': ['2023-10-25', '2026-10-24'], 'source': 'recommended'},
               })}
    route = {'intent': 'amend', 'period_start': '2024-09-01', 'period_end': '2026-10-24'}
    out = asyncio.run(ssr._handle_pool_active(session, '区间改成2024年9月开始', 'd', None, route))
    assert session['period_start'] == '2024-09-01'
    assert session['status'] == 'pending_confirm'
    assert '2024-09-01' in out and '补充条件' in out
    assert session['amend_notes'][-1] == '区间改成2024年9月开始'

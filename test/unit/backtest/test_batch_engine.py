"""BatchBacktestEngine 单元测试

mock BacktestEngine.run，验证：
- 并发执行
- 失败隔离
- 进度回调
- 结果聚合
- 状态机（completed / partial / failed）
"""
import threading
import time
from unittest.mock import patch, MagicMock

import pytest


# ============================================================
# 辅助函数测试
# ============================================================

def test_auto_detect_asset_type():
    """识别各类资产代码"""
    from backtest.batch_engine import _auto_detect_asset_type
    # ETF
    assert _auto_detect_asset_type('510300') == 'etf'
    assert _auto_detect_asset_type('159915') == 'etf'
    assert _auto_detect_asset_type('512760') == 'etf'
    assert _auto_detect_asset_type('588000') == 'etf'
    # 深证指数
    assert _auto_detect_asset_type('399001') == 'index'
    assert _auto_detect_asset_type('399006') == 'index'
    # 板块
    assert _auto_detect_asset_type('board_whitelist') == 'board'
    # 个股
    assert _auto_detect_asset_type('600519') == 'stock'
    assert _auto_detect_asset_type('000858') == 'stock'  # 五粮液（深市主板个股）
    assert _auto_detect_asset_type('000001') == 'stock'  # 默认按 stock 处理（数据层会自动区分）
    assert _auto_detect_asset_type('000300') == 'stock'  # 沪深300 也按 stock 处理，由数据层决定
    assert _auto_detect_asset_type('300750') == 'stock'
    # 边界
    assert _auto_detect_asset_type('') == 'stock'
    assert _auto_detect_asset_type(None) == 'stock'


def test_mean():
    """安全求平均"""
    from backtest.batch_engine import _mean
    assert _mean([1, 2, 3]) == 2.0
    assert _mean([0.1, 0.2]) == pytest.approx(0.15)
    assert _mean([]) == 0.0
    assert _mean([-1, 1]) == 0.0


def test_pick_metrics():
    """从 BacktestEngine 返回结果中提取批量关注的指标"""
    from backtest.batch_engine import _pick_metrics
    result = {
        'run_id': 'R1',
        'status': 'completed',
        'total_return': 0.234,
        'annual_return': 0.15,
        'sharpe_ratio': 1.2,
        'max_drawdown': -0.1,
        'win_rate': 0.6,
        'trade_count': 12,
        'final_equity': 123400.0,
        # 批量层不关心的字段
        'equity_curve': [],
        'trades': [],
        'monthly_returns': {},
    }
    picked = _pick_metrics(result)
    assert picked == {
        'total_return': 0.234,
        'annual_return': 0.15,
        'sharpe_ratio': 1.2,
        'max_drawdown': -0.1,
        'win_rate': 0.6,
        'trade_count': 12,
        'final_equity': 123400.0,
    }
    # 空字典应不抛异常
    assert _pick_metrics({}) == {
        'total_return': None,
        'annual_return': None,
        'sharpe_ratio': None,
        'max_drawdown': None,
        'win_rate': None,
        'trade_count': None,
        'final_equity': None,
    }


def test_calc_duration_seconds():
    """耗时计算"""
    from backtest.batch_engine import _calc_duration_seconds
    assert _calc_duration_seconds('2024-01-01 10:00:00', '2024-01-01 10:00:30') == 30.0
    assert _calc_duration_seconds('2024-01-01 10:00:00', '2024-01-01 10:01:30') == 90.0
    # 异常输入返回 0
    assert _calc_duration_seconds('', '2024-01-01 10:00:00') == 0.0
    assert _calc_duration_seconds('invalid', '2024-01-01 10:00:00') == 0.0


# ============================================================
# BatchBacktestEngine 单元测试
# ============================================================

@pytest.fixture
def setup_test_db(tmp_path):
    """临时 backtest.db，让 update_batch / update_batch_item 可用"""
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _make_fake_engine_result(run_id, total_return=0.1, annual_return=0.08,
                             sharpe_ratio=1.0, max_drawdown=-0.05,
                             win_rate=0.55, trade_count=10, final_equity=110000.0):
    """构造 BacktestEngine.run 的伪返回值"""
    return {
        'run_id': run_id,
        'status': 'completed',
        'total_return': total_return,
        'annual_return': annual_return,
        'sharpe_ratio': sharpe_ratio,
        'max_drawdown': max_drawdown,
        'win_rate': win_rate,
        'trade_count': trade_count,
        'final_equity': final_equity,
        'equity_curve': [],
        'trades': [],
    }


def test_resolve_concurrency(setup_test_db):
    """并发数受 max_concurrency 约束"""
    from backtest.batch_engine import BatchBacktestEngine, MAX_CONCURRENCY_HARD_LIMIT

    # 默认值
    e = BatchBacktestEngine()
    assert e.max_concurrency == MAX_CONCURRENCY_HARD_LIMIT

    # 用户指定
    e = BatchBacktestEngine(max_concurrency=3)
    assert e.max_concurrency == 3

    # 超上限被截断
    e = BatchBacktestEngine(max_concurrency=100)
    assert e.max_concurrency == MAX_CONCURRENCY_HARD_LIMIT

    # 小于 1 被拉到 1
    e = BatchBacktestEngine(max_concurrency=0)
    assert e.max_concurrency == 1

    # _resolve_concurrency 方法
    e = BatchBacktestEngine(max_concurrency=4)
    assert e._resolve_concurrency(3) == 3
    assert e._resolve_concurrency(10) == 4  # 受 max_concurrency 约束
    assert e._resolve_concurrency(0) == 1   # 下限 1
    assert e._resolve_concurrency(None) == 3  # 默认 3
    assert e._resolve_concurrency('abc') == 3  # 异常类型降级 3


def test_aggregate_all_completed(setup_test_db):
    """聚合：全部成功"""
    from backtest.batch_engine import BatchBacktestEngine
    e = BatchBacktestEngine()
    items = [
        {'code': '600519', 'status': 'completed', 'total_return': 0.2, 'annual_return': 0.15,
         'sharpe_ratio': 1.2, 'max_drawdown': -0.1, 'win_rate': 0.6, 'trade_count': 10},
        {'code': '000858', 'status': 'completed', 'total_return': -0.1, 'annual_return': -0.05,
         'sharpe_ratio': 0.8, 'max_drawdown': -0.15, 'win_rate': 0.5, 'trade_count': 8},
    ]
    agg = e._aggregate(items)
    assert agg['avg_total_return'] == pytest.approx(0.05)
    assert agg['avg_annual_return'] == pytest.approx(0.05)
    assert agg['avg_sharpe'] == pytest.approx(1.0)
    assert agg['avg_max_drawdown'] == pytest.approx(-0.125)
    assert agg['avg_win_rate'] == pytest.approx(0.55)
    assert agg['profit_count'] == 1
    assert agg['loss_count'] == 1


def test_aggregate_with_failures(setup_test_db):
    """聚合：含失败 item，仅对 completed 聚合"""
    from backtest.batch_engine import BatchBacktestEngine
    e = BatchBacktestEngine()
    items = [
        {'code': 'A', 'status': 'completed', 'total_return': 0.3, 'annual_return': 0.2,
         'sharpe_ratio': 1.5, 'max_drawdown': -0.05, 'win_rate': 0.7, 'trade_count': 5},
        {'code': 'B', 'status': 'failed', 'error': '数据不足'},
        {'code': 'C', 'status': 'completed', 'total_return': -0.2, 'annual_return': -0.1,
         'sharpe_ratio': None, 'max_drawdown': -0.2, 'win_rate': 0.4, 'trade_count': 3},
    ]
    agg = e._aggregate(items)
    # 只对 A 和 C 聚合
    assert agg['avg_total_return'] == pytest.approx(0.05)  # (0.3 + -0.2) / 2
    assert agg['avg_annual_return'] == pytest.approx(0.05)  # (0.2 + -0.1) / 2
    # sharpe_ratio: 只对非 None 求平均 → [1.5]，C 的 None 跳过
    assert agg['avg_sharpe'] == pytest.approx(1.5)
    assert agg['avg_max_drawdown'] == pytest.approx(-0.125)
    assert agg['avg_win_rate'] == pytest.approx(0.55)
    assert agg['profit_count'] == 1
    assert agg['loss_count'] == 1


def test_aggregate_all_failed(setup_test_db):
    """聚合：全部失败 → 全 0"""
    from backtest.batch_engine import BatchBacktestEngine
    e = BatchBacktestEngine()
    items = [
        {'code': 'A', 'status': 'failed', 'error': 'x'},
        {'code': 'B', 'status': 'failed', 'error': 'y'},
    ]
    agg = e._aggregate(items)
    assert agg['avg_total_return'] == 0.0
    assert agg['avg_sharpe'] == 0.0
    assert agg['profit_count'] == 0
    assert agg['loss_count'] == 0


# ============================================================
# run() 完整流程（mock BacktestEngine）
# ============================================================

def test_run_all_success(setup_test_db):
    """run: 全部成功 → status=completed"""
    from backtest.batch_engine import BatchBacktestEngine
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, get_batch, get_batch_items
    )

    # 准备 batch + items
    create_batch(
        batch_id='BTB-RUN-OK', strategy_id='s1', strategy_name='',
        params={}, config={'concurrency': 2}, codes=['600519', '000858', '510300'],
        initial_cash=100000, start_date='2024-01-01', end_date='2024-12-31',
    )
    create_batch_items('BTB-RUN-OK', [
        {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-001'},
        {'code': '000858', 'asset_type': 'stock', 'run_id': 'R-002'},
        {'code': '510300', 'asset_type': 'etf', 'run_id': 'R-003'},
    ])

    # mock BacktestEngine
    def fake_run(config, run_id=None, progress_callback=None):
        return _make_fake_engine_result(
            run_id, total_return=0.1 if '600519' in config['code'] else -0.05
        )

    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = fake_run

        engine = BatchBacktestEngine(max_concurrency=5)
        progress_events = []
        result = engine.run(
            codes=['600519', '000858', '510300'],
            config={'strategy_id': 's1', 'concurrency': 2},
            batch_id='BTB-RUN-OK',
            run_ids=['R-001', 'R-002', 'R-003'],
            progress_callback=lambda bid, code, status, cur, total: progress_events.append(
                (bid, code, status, cur, total)
            ),
        )

    # 返回值
    assert result['batch_id'] == 'BTB-RUN-OK'
    assert result['status'] == 'completed'
    assert result['success_count'] == 3
    assert result['failed_count'] == 0
    assert len(result['items']) == 3

    # 进度回调：3 次
    assert len(progress_events) == 3
    # 所有进度事件的 batch_id 都是 BTB-RUN-OK
    assert all(e[0] == 'BTB-RUN-OK' for e in progress_events)
    # current 从 1..3
    assert sorted(e[3] for e in progress_events) == [1, 2, 3]
    # total 始终是 3
    assert all(e[4] == 3 for e in progress_events)

    # 落库：batch 状态
    batch = get_batch('BTB-RUN-OK')
    assert batch['status'] == 'completed'
    assert batch['avg_total_return'] == pytest.approx((0.1 + -0.05 + -0.05) / 3)
    assert batch['profit_count'] == 1  # 仅 600519
    assert batch['loss_count'] == 2   # 000858 + 510300
    assert batch['duration_seconds'] is not None

    # 落库：items 状态
    items = get_batch_items('BTB-RUN-OK')
    assert len(items) == 3
    for it in items:
        assert it['status'] == 'completed'
        assert it['total_return'] is not None


def test_run_partial_failure(setup_test_db):
    """run: 部分失败 → status=partial"""
    from backtest.batch_engine import BatchBacktestEngine
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, get_batch, get_batch_items
    )

    create_batch(
        batch_id='BTB-RUN-PART', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['A', 'B', 'C'],
        initial_cash=100000, start_date='', end_date='',
    )
    create_batch_items('BTB-RUN-PART', [
        {'code': 'A', 'asset_type': 'stock', 'run_id': 'R-A'},
        {'code': 'B', 'asset_type': 'stock', 'run_id': 'R-B'},
        {'code': 'C', 'asset_type': 'stock', 'run_id': 'R-C'},
    ])

    def fake_run(config, run_id=None, progress_callback=None):
        code = config['code']
        if code == 'B':
            raise ValueError('B 数据不足')
        if code == 'C':
            raise RuntimeError('C 策略异常')
        return _make_fake_engine_result(run_id, total_return=0.15)

    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = fake_run

        engine = BatchBacktestEngine()
        result = engine.run(
            codes=['A', 'B', 'C'],
            config={'strategy_id': 's1'},
            batch_id='BTB-RUN-PART',
            run_ids=['R-A', 'R-B', 'R-C'],
        )

    assert result['status'] == 'partial'
    assert result['success_count'] == 1
    assert result['failed_count'] == 2
    # 聚合只对 A 算
    assert result['aggregates']['avg_total_return'] == pytest.approx(0.15)
    assert result['aggregates']['profit_count'] == 1
    assert result['aggregates']['loss_count'] == 0

    # 落库
    batch = get_batch('BTB-RUN-PART')
    assert batch['status'] == 'partial'
    items = {it['code']: it for it in get_batch_items('BTB-RUN-PART')}
    assert items['A']['status'] == 'completed'
    assert items['B']['status'] == 'failed'
    assert 'B 数据不足' in items['B']['error_message']
    assert items['C']['status'] == 'failed'
    assert 'C 策略异常' in items['C']['error_message']


def test_run_all_failed(setup_test_db):
    """run: 全部失败 → status=failed"""
    from backtest.batch_engine import BatchBacktestEngine
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, get_batch
    )

    create_batch(
        batch_id='BTB-RUN-FAIL', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['A', 'B'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-RUN-FAIL', [
        {'code': 'A', 'run_id': 'R-A'},
        {'code': 'B', 'run_id': 'R-B'},
    ])

    def fake_run(config, run_id=None, progress_callback=None):
        raise ValueError('all dead')

    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = fake_run
        engine = BatchBacktestEngine()
        result = engine.run(
            codes=['A', 'B'],
            config={'strategy_id': 's1'},
            batch_id='BTB-RUN-FAIL',
            run_ids=['R-A', 'R-B'],
        )

    assert result['status'] == 'failed'
    assert result['success_count'] == 0
    assert result['failed_count'] == 2
    batch = get_batch('BTB-RUN-FAIL')
    assert batch['status'] == 'failed'


def test_run_length_mismatch_raises(setup_test_db):
    """codes 与 run_ids 长度不一致应抛 ValueError"""
    from backtest.batch_engine import BatchBacktestEngine
    engine = BatchBacktestEngine()
    with pytest.raises(ValueError, match='长度不一致'):
        engine.run(
            codes=['A', 'B'],
            config={},
            batch_id='BTB-X',
            run_ids=['R-A'],  # 长度不一致
        )


def test_run_concurrent_execution(setup_test_db):
    """验证 ThreadPoolExecutor 真的并发：3 个任务各自 sleep 0.3s，
    总耗时应远小于串行的 0.9s（用线程名验证并发）"""
    from backtest.batch_engine import BatchBacktestEngine
    from utils.cache.backtest_db import (
        create_batch, create_batch_items
    )

    create_batch(
        batch_id='BTB-RUN-CONC', strategy_id='s1', strategy_name='',
        params={}, config={'concurrency': 3}, codes=['A', 'B', 'C'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-RUN-CONC', [
        {'code': 'A', 'run_id': 'R-A'},
        {'code': 'B', 'run_id': 'R-B'},
        {'code': 'C', 'run_id': 'R-C'},
    ])

    thread_names = []
    lock = threading.Lock()

    def fake_run(config, run_id=None, progress_callback=None):
        with lock:
            thread_names.append(threading.current_thread().name)
        time.sleep(0.3)
        return _make_fake_engine_result(run_id)

    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = fake_run
        engine = BatchBacktestEngine(max_concurrency=5)
        start = time.time()
        engine.run(
            codes=['A', 'B', 'C'],
            config={'concurrency': 3},
            batch_id='BTB-RUN-CONC',
            run_ids=['R-A', 'R-B', 'R-C'],
        )
        elapsed = time.time() - start

    # 3 个任务并发 0.3s 应在 0.6s 内完成（容忍调度开销）
    assert elapsed < 0.9, f'未并发: elapsed={elapsed:.2f}s'
    # 3 个任务在不同的线程中执行
    assert len(set(thread_names)) == 3


def test_run_progress_callback_invocation(setup_test_db):
    """progress_callback 签名：(batch_id, code, status, current, total)"""
    from backtest.batch_engine import BatchBacktestEngine
    from utils.cache.backtest_db import create_batch, create_batch_items

    create_batch(
        batch_id='BTB-RUN-PROG', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['A', 'B'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-RUN-PROG', [
        {'code': 'A', 'run_id': 'R-A'},
        {'code': 'B', 'run_id': 'R-B'},
    ])

    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = lambda config, run_id=None, progress_callback=None: \
            _make_fake_engine_result(run_id)
        engine = BatchBacktestEngine()
        events = []
        engine.run(
            codes=['A', 'B'],
            config={'strategy_id': 's1'},
            batch_id='BTB-RUN-PROG',
            run_ids=['R-A', 'R-B'],
            progress_callback=lambda *args: events.append(args),
        )

    # 2 次 progress 回调
    assert len(events) == 2
    # 每次回调 5 个参数
    for ev in events:
        assert len(ev) == 5
        assert ev[0] == 'BTB-RUN-PROG'  # batch_id
        assert ev[1] in ('A', 'B')       # code
        assert ev[2] in ('completed', 'failed')  # status
        assert ev[3] in (1, 2)            # current
        assert ev[4] == 2                 # total
    # current 应该是 1 和 2
    assert sorted(ev[3] for ev in events) == [1, 2]


def test_run_config_isolation(setup_test_db):
    """每个 code 拿到的 config 是独立的拷贝（不污染 base_config）"""
    from backtest.batch_engine import BatchBacktestEngine
    from utils.cache.backtest_db import create_batch, create_batch_items

    create_batch(
        batch_id='BTB-RUN-ISO', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['600519', '510300'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-RUN-ISO', [
        {'code': '600519', 'run_id': 'R-1'},
        {'code': '510300', 'run_id': 'R-2'},
    ])

    received_configs = []
    def fake_run(config, run_id=None, progress_callback=None):
        received_configs.append({k: v for k, v in config.items()})
        return _make_fake_engine_result(run_id)

    base_config = {'strategy_id': 's1', 'initial_cash': 100000}
    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = fake_run
        engine = BatchBacktestEngine()
        engine.run(
            codes=['600519', '510300'],
            config=base_config,
            batch_id='BTB-RUN-ISO',
            run_ids=['R-1', 'R-2'],
        )

    # base_config 不应被污染（没多出 code/asset_type 字段）
    assert 'code' not in base_config
    assert 'asset_type' not in base_config

    # 每个 code 都收到了正确的 code + asset_type
    assert len(received_configs) == 2
    cfgs_by_code = {c['code']: c for c in received_configs}
    assert cfgs_by_code['600519']['asset_type'] == 'stock'
    assert cfgs_by_code['510300']['asset_type'] == 'etf'
    # 共享字段保留
    assert cfgs_by_code['600519']['strategy_id'] == 's1'
    assert cfgs_by_code['510300']['initial_cash'] == 100000


def test_run_error_message_truncation(setup_test_db):
    """失败的 error_message 应被截断到 ERROR_MESSAGE_MAX_LEN"""
    from backtest.batch_engine import BatchBacktestEngine, ERROR_MESSAGE_MAX_LEN
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, get_batch_item_by_code
    )

    create_batch(
        batch_id='BTB-RUN-TRUNC', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['A'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-RUN-TRUNC', [
        {'code': 'A', 'run_id': 'R-A'},
    ])

    long_msg = 'X' * 2000
    with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
        MockEngine.return_value.run.side_effect = lambda config, **kw: (_ for _ in ()).throw(
            ValueError(long_msg)
        )
        engine = BatchBacktestEngine()
        engine.run(
            codes=['A'],
            config={},
            batch_id='BTB-RUN-TRUNC',
            run_ids=['R-A'],
        )

    item = get_batch_item_by_code('BTB-RUN-TRUNC', 'A')
    assert item['status'] == 'failed'
    assert len(item['error_message']) == ERROR_MESSAGE_MAX_LEN

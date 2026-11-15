"""策略一生聚合服务单测（Plan 4 任务 2 步骤 2.7，8 例）。

不连真库：每个用例把 backtest.db 指向 tmp_path 下的临时文件并建表（范式同
test_strategy_session_store.py），前瞻统计与盯盘规则一律打桩——那两个域有各自的真实库，
不该在本用例里被读到。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── 桩：前瞻统计（signal_eval_service 要读真实信号库 + K 线） ──────────────

_FAKE_FORWARD = {
    'signal_count': 0, 'mature_count': 0, 'pending_count': 0, 'holding_days': 5,
    'win_rate': None, 'wilson_ci': None, 'avg_return': None, 'expectancy': None,
    'payoff': None, 'days_tracked': 0, 'first_signal_at': '', 'signals': [],
}


def _fake_build_by_strategy(strategy_id, cost_pct=0.0, holding_days=5, today=''):
    return dict(_FAKE_FORWARD, strategy_id=strategy_id)


# ── 桩：盯盘规则（MonitorStore 会 open 真实 monitor_alerts 库） ────────────

class _FakeMonitorStore:
    """盯盘规则桩：默认空表，用例用 monkeypatch 注入 rules。"""

    rules: list = []

    def __init__(self, *args, **kwargs):
        pass

    def list_rules(self, enabled_only: bool = False) -> list:
        return list(type(self).rules)


@pytest.fixture(autouse=True)
def tmp_backtest_db(tmp_path, monkeypatch):
    """回测库 → 临时文件（绝不碰 db/backtest.db），并打桩两个域外依赖。"""
    from config import Config
    test_db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()

    import server.signal_eval_service as signal_service
    monkeypatch.setattr(signal_service, 'build_by_strategy', _fake_build_by_strategy)

    import server.monitoring.store as monitor_store
    monkeypatch.setattr(monitor_store, 'MonitorStore', _FakeMonitorStore)

    monkeypatch.setattr(_FakeMonitorStore, 'rules', [])
    return test_db


# ── 造数辅助 ─────────────────────────────────────────────────────────────

def _make_strategy(strategy_id: str = 'stg-life', strategy_hash: str = '') -> str:
    from utils.cache.backtest_db import upsert_strategy, update_strategy_hash
    upsert_strategy(strategy_id, '生命周期策略', '描述', 'custom', '[]')
    if strategy_hash:
        update_strategy_hash(strategy_id, strategy_hash)
    return strategy_id


def _add_run(run_id: str, strategy_id: str, code: str = '000001', *,
             health: str = 'ok', trade_sample_ok: int = 1, trade_count: int = 7,
             origin: str = 'manual', ref_id: str = '', win_rate: float = 0.5) -> str:
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result
    create_backtest_run(run_id, {
        'code': code, 'strategy_id': strategy_id, 'strategy_name': '生命周期策略',
        'stock_name': '平安银行', 'start_date': '2024-01-01', 'end_date': '2024-12-31',
    }, origin=origin, ref_id=ref_id)
    save_backtest_result(run_id, {
        'total_return': 0.2, 'annual_return': 0.18, 'sharpe_ratio': 1.1,
        'max_drawdown': -0.1, 'win_rate': win_rate, 'trade_count': trade_count,
        'health': health, 'health_detail': '', 'data_bars': 240, 'expected_bars': 240,
        'coverage_pct': 100.0, 'trade_sample_ok': trade_sample_ok,
    })
    return run_id


def _add_iter(strategy_hash: str, round_no: int, is_metrics: dict) -> int:
    """同一研发 run 下插一轮迭代（is_metrics 以 JSON 字符串落库，读回应是 dict）。"""
    from utils.cache.backtest_db import (create_strategy_run, get_strategy_run,
                                         insert_strategy_iter)
    run_uuid = 'sr-life-1'
    if get_strategy_run(run_uuid) is None:
        create_strategy_run(run_uuid, '想法', '000001', '2020-2024')
    return insert_strategy_iter(run_uuid, round_no, '假设', '{}', strategy_hash, '',
                                1, None, is_metrics, {'sharpe': 0.9})


def _build(strategy_id: str = 'stg-life', **kwargs):
    from server.backtest_service import build_strategy_lifecycle
    return build_strategy_lifecycle(strategy_id, **kwargs)


# ── 1 ────────────────────────────────────────────────────────────────────

def test_strategy_not_found_returns_none(tmp_backtest_db):
    assert _build('nope') is None


# ── 2 ────────────────────────────────────────────────────────────────────

def test_empty_strategy_has_all_blocks_and_not_verified(tmp_backtest_db):
    _make_strategy()
    data = _build()
    assert data is not None
    assert data['strategy']['strategy_id'] == 'stg-life'
    assert data['strategy']['strategy_hash'] == ''
    assert data['runs'] == [] and data['runs_total'] == 0
    assert data['verified'] is False
    assert data['evals'] == {'evaluated': 0, 'counts': {}, 'latest_verdict': '', 'rows': []}
    assert data['iterations'] == []
    assert data['forward']['signal_count'] == 0
    assert data['monitor'] == {'rules': [], 'matched': 0}


# ── 3 ────────────────────────────────────────────────────────────────────

def test_verified_true_when_health_ok_and_sample_enough(tmp_backtest_db):
    _make_strategy()
    _add_run('BT-L1', 'stg-life', health='data_gap', trade_sample_ok=0, trade_count=0)
    _add_run('BT-L2', 'stg-life', health='low_sample', trade_sample_ok=1, trade_count=7)
    _add_run('BT-L3', 'stg-life', health='ok', trade_sample_ok=1, trade_count=7)
    data = _build()
    assert data['runs_total'] == 3
    assert data['verified'] is True


# ── 4 ────────────────────────────────────────────────────────────────────

def test_verified_false_when_trade_count_below_min(tmp_backtest_db):
    _make_strategy()
    _add_run('BT-L4', 'stg-life', health='ok', trade_sample_ok=1, trade_count=3)
    data = _build()
    assert data['verified'] is False
    assert data['runs'][0]['trade_count'] == 3


# ── 5 ────────────────────────────────────────────────────────────────────

def test_data_gap_run_passes_health_through_and_not_verified(tmp_backtest_db):
    _make_strategy()
    _add_run('BT-L5', 'stg-life', health='data_gap', trade_sample_ok=1, trade_count=9)
    data = _build()
    assert data['verified'] is False
    assert data['runs'][0]['health'] == 'data_gap'


# ── 6 ────────────────────────────────────────────────────────────────────

def test_iterations_found_by_hash_and_metrics_deserialized(tmp_backtest_db):
    _make_strategy(strategy_hash='hash-life-1')
    _add_iter('hash-life-1', 1, {'sharpe': 1.2, 'trades': 30})
    _add_iter('hash-life-1', 2, {'sharpe': 1.5, 'trades': 40})
    data = _build()
    assert len(data['iterations']) == 2
    assert data['iterations'][0]['is_metrics'] == {'sharpe': 1.2, 'trades': 30}
    assert isinstance(data['iterations'][1]['is_metrics'], dict)
    assert data['iterations'][1]['oos_metrics'] == {'sharpe': 0.9}


# ── 7 ────────────────────────────────────────────────────────────────────

def test_eval_read_error_does_not_bubble(tmp_backtest_db, monkeypatch):
    _make_strategy()
    _add_run('BT-L7', 'stg-life')

    def _boom(run_id):
        raise RuntimeError('backtest_eval 表炸了')

    monkeypatch.setattr('utils.cache.backtest_db.get_backtest_eval', _boom)
    data = _build()
    assert data['evals']['evaluated'] == 0
    assert data['evals']['counts'] == {}


# ── 8 ────────────────────────────────────────────────────────────────────

def test_monitor_matched_only_strategy_research_rules(tmp_backtest_db, monkeypatch):
    _make_strategy()
    _add_run('BT-L8', 'stg-life', code='000001')
    monkeypatch.setattr(_FakeMonitorStore, 'rules', [
        {'id': 1, 'stock_code': '000001', 'stock_name': '平安银行',
         'rule_type': 'price_up', 'threshold': 3.0, 'enabled': 1,
         'remark': '研发落地', 'source': 'strategy_research', 'created_at': '2026-01-01 00:00:00'},
        {'id': 2, 'stock_code': '600519', 'stock_name': '贵州茅台',
         'rule_type': 'price_up', 'threshold': 3.0, 'enabled': 1,
         'remark': '', 'source': 'strategy_research', 'created_at': '2026-01-01 00:00:00'},
        {'id': 3, 'stock_code': '000001', 'stock_name': '平安银行',
         'rule_type': 'price_down', 'threshold': 3.0, 'enabled': 1,
         'remark': '手工加的', 'source': 'manual', 'created_at': '2026-01-01 00:00:00'},
    ])
    data = _build()
    assert data['monitor']['matched'] == 1
    assert data['monitor']['rules'][0]['id'] == 1
    assert data['monitor']['rules'][0]['stock_code'] == '000001'


# ── 9 ────────────────────────────────────────────────────────────────────

def test_legacy_percent_win_rate_is_normalized(tmp_backtest_db):
    """存量行胜率是百分数刻度（9.0 = 9%），详情页不能显示成 1100%（Plan 3 验收同款问题）。"""
    _make_strategy()
    _add_run('BT-L9-OLD', 'stg-life', code='600519', win_rate=9.0)
    _add_run('BT-L9-NEW', 'stg-life', code='000001', win_rate=0.61)
    data = _build()
    got = {r['run_id']: r['win_rate'] for r in data['runs']}
    assert got['BT-L9-OLD'] == pytest.approx(0.09)
    assert got['BT-L9-NEW'] == pytest.approx(0.61)

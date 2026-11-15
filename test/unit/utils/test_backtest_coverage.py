"""回测覆盖率巡检单元测试（Plan 4 任务 6）。

5 例：缺列（OperationalError）、窗口内无回测、全部达标、两条低覆盖按覆盖率升序、NULL 不算低覆盖。

⚠️ 口径：`backtest_results.coverage_pct` 落库是 **0–1 比值**（backtest/health.py），
本模块对外统一输出**百分数**（阈值 LOW_COVERAGE_PCT=95.0 也是百分数）——
所以夹具写 0.62/0.78，断言读 62.0/78.0。这条口径曾被写错（按百分数解读），
用例 3（1.0 不算低覆盖）与用例 4（0.62 → 62.0）就是防回归的锁。
打桩方式：monkeypatch `utils.cache.backtest_db.get_backtest_db` 换成内存 sqlite（最小两表），
**不碰真实 db/backtest.db**。
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from utils.data_monitor.backtest_coverage import collect_coverage_check

_RUNS_DDL = """
CREATE TABLE backtest_runs (
    run_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    stock_name TEXT,
    strategy_id TEXT NOT NULL,
    origin TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL
)"""

_RESULTS_DDL = """
CREATE TABLE backtest_results (
    run_id TEXT PRIMARY KEY,
    coverage_pct REAL,
    health TEXT,
    data_bars INTEGER,
    expected_bars INTEGER,
    created_at TEXT NOT NULL
)"""

# 缺 coverage_pct 列：模拟 Plan 1 尚未落库的旧库
_RESULTS_DDL_LEGACY = """
CREATE TABLE backtest_results (
    run_id TEXT PRIMARY KEY,
    health TEXT,
    data_bars INTEGER,
    expected_bars INTEGER,
    created_at TEXT NOT NULL
)"""


def _stamp(days_ago: int = 1) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def stub_db(monkeypatch):
    """返回建库工厂：内存 sqlite + 打桩连接，测试结束统一关闭。"""
    conns = []

    def _make(with_coverage: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute(_RUNS_DDL)
        conn.execute(_RESULTS_DDL if with_coverage else _RESULTS_DDL_LEGACY)

        @contextmanager
        def _ctx():
            yield conn

        import utils.cache.backtest_db as bdb
        monkeypatch.setattr(bdb, 'get_backtest_db', _ctx)
        conns.append(conn)
        return conn

    yield _make
    for c in conns:
        c.close()


def _add_run(conn, run_id, coverage_pct, *, days_ago=1, code='600519',
             stock_name='贵州茅台', strategy_id='ma_cross', origin='manual',
             health='data_gap', data_bars=200, expected_bars=300, with_coverage=True):
    """插一条 run + 一条 result（coverage_pct=None 时写 NULL）。

    with_coverage=False 时按「旧库」写法省略 coverage_pct 列（该库本就没这列）。
    """
    created_at = _stamp(days_ago)
    conn.execute(
        'INSERT INTO backtest_runs (run_id, code, stock_name, strategy_id, origin, created_at)'
        ' VALUES (?, ?, ?, ?, ?, ?)',
        (run_id, code, stock_name, strategy_id, origin, created_at))
    if with_coverage:
        conn.execute(
            'INSERT INTO backtest_results (run_id, coverage_pct, health, data_bars, expected_bars, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            (run_id, coverage_pct, health, data_bars, expected_bars, created_at))
    else:
        conn.execute(
            'INSERT INTO backtest_results (run_id, health, data_bars, expected_bars, created_at)'
            ' VALUES (?, ?, ?, ?, ?)',
            (run_id, health, data_bars, expected_bars, created_at))
    conn.commit()


def test_missing_column_returns_error_without_raising(stub_db):
    """1. 表/列缺失（OperationalError）→ error 非空、low_count == 0、不抛。"""
    conn = stub_db(with_coverage=False)
    _add_run(conn, 'run-legacy', 0.88, with_coverage=False)

    out = collect_coverage_check()

    assert out['error']                        # 缺列原因写进 error
    assert 'coverage_pct' in out['error']
    assert out['low_count'] == 0
    assert out['low_runs'] == []
    assert '落库' in out['hint']


def test_no_backtest_within_window(stub_db):
    """2. 窗口内无回测（窗口外的旧记录不算）→ checked == 0，hint 含「没有回测记录」。"""
    conn = stub_db()
    _add_run(conn, 'run-old', 0.50, days_ago=100)   # 30 天窗口之外

    out = collect_coverage_check(days=30)

    assert out['checked'] == 0
    assert out['low_count'] == 0
    assert '没有回测记录' in out['hint']


def test_all_above_threshold(stub_db):
    """3. 有回测但都 ≥ 95%（比值 1.0 / 0.965）→ low_count == 0，hint 含「没有覆盖率低于」。"""
    conn = stub_db()
    _add_run(conn, 'run-a', 1.0)      # 100%（比值 1.0）——不得被判成低覆盖
    _add_run(conn, 'run-b', 0.965)

    out = collect_coverage_check()

    assert out['checked'] == 2
    assert out['low_count'] == 0
    assert '没有覆盖率低于' in out['hint']
    assert '95%' in out['hint']


def test_low_coverage_runs_sorted_ascending(stub_db):
    """4. 两条低于阈值（比值 0.78 / 0.62）→ 输出百分数 78.0 / 62.0，62% 在前（升序最该先看）。"""
    conn = stub_db()
    _add_run(conn, 'run-78', 0.78, code='000001', stock_name='平安银行', origin=None)
    _add_run(conn, 'run-62', 0.62, code='600519', stock_name='贵州茅台', strategy_id='boll')

    out = collect_coverage_check()

    assert out['checked'] == 2
    assert out['low_count'] == 2
    assert [r['coverage_pct'] for r in out['low_runs']] == [62.0, 78.0]
    assert [r['run_id'] for r in out['low_runs']] == ['run-62', 'run-78']
    first = out['low_runs'][0]
    assert first['code'] == '600519' and first['stock_name'] == '贵州茅台'
    assert first['strategy_id'] == 'boll' and first['health'] == 'data_gap'
    assert first['data_bars'] == 200 and first['expected_bars'] == 300
    assert out['low_runs'][1]['origin'] == 'manual'    # NULL origin 回退默认值
    assert '这些结论要打折看' in out['hint']


def test_failed_runs_are_not_reported_as_low_coverage(stub_db):
    """6. 资金不足（invalid_params）/执行出错（error）的行 coverage 恒为 0，
    属「没跑起来」而非「数据不全」，不得出现在低覆盖清单里。"""
    conn = stub_db()
    _add_run(conn, 'run-nomoney', 0.0, health='invalid_params')
    _add_run(conn, 'run-error', 0.0, health='error')
    _add_run(conn, 'run-gap', 0.62, health='data_gap')

    out = collect_coverage_check()

    assert out['checked'] == 3                 # 分母仍是全部有结果的行
    assert [r['run_id'] for r in out['low_runs']] == ['run-gap']


def test_null_coverage_is_not_low_coverage(stub_db):
    """5. coverage_pct 为 NULL → 计入 checked，但不计入 low_runs。"""
    conn = stub_db()
    _add_run(conn, 'run-null', None)
    _add_run(conn, 'run-low', 0.60)

    out = collect_coverage_check()

    assert out['checked'] == 2
    assert out['low_count'] == 1
    assert [r['run_id'] for r in out['low_runs']] == ['run-low']

"""回测数据就绪度 get_backtest_readiness 单测（Plan 4 任务 5 步骤 5.5）。

4 例：空库 / NULL 不进均值 / 缺口计数 / 补数与重跑。
全程只操作 tmp_path 下的假 SQLite，monkeypatch utils.cache.backtest_db.get_backtest_db，
不碰真实 db/backtest.db（只读也不做，纯假库）。

口径备注（与计划 5.5 表格的偏差，详见任务回报）：
- gap_runs 的口径是 `backtest_results.health == 'data_gap'`（步骤 2.4 SQL 原文），
  不是「coverage_pct < 95」现算 —— health 已由 backtest/health.py::resolve_health 按
  DEFAULT_MIN_COVERAGE 判定后落库，两者同义；
- `backtest_results.coverage_pct` 落库是 0–1 比值（backtest/health.py:57 的
  `round(covered / len(baseline), 4)`），get_backtest_readiness 对外 ×100 成百分数，
  故造数用 0.90 断言 90.0。
"""
import contextlib
import sqlite3
from datetime import datetime, timedelta

import pytest

import utils.cache.backtest_db as bdb

_FMT = '%Y-%m-%d %H:%M:%S'


def _ts(days_ago: float = 0, hours_ago: float = 0) -> str:
    """相对当前时间的 created_at 字面量（库里就是 '%Y-%m-%d %H:%M:%S' 字符串比较）。"""
    return (datetime.now() - timedelta(days=days_ago, hours=hours_ago)).strftime(_FMT)


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    """假 backtest.db：只建 get_backtest_readiness 真正查询的三张表。

    ⚠️ backtest_results 必须有 health 列：步骤 2.4 的 SQL 会
    `SUM(CASE WHEN res.health = 'data_gap' …)`，缺列时 sqlite3 抛 OperationalError，
    被函数里的 `except sqlite3.OperationalError: pass` 吞掉 → 所有计数静默归零（假绿）。
    """
    path = tmp_path / 'bt.db'
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE backtest_runs (run_id TEXT, origin TEXT, ref_id TEXT, created_at TEXT);
        CREATE TABLE backtest_results (run_id TEXT, coverage_pct REAL, health TEXT);
        CREATE TABLE data_tasks (task_id TEXT, origin TEXT, ref_id TEXT, status TEXT, created_at TEXT);
        """
    )
    conn.commit()
    conn.close()

    @contextlib.contextmanager
    def _ctx():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.commit()
            c.close()

    monkeypatch.setattr(bdb, 'get_backtest_db', _ctx)
    return path


def _add_run(path, run_id, coverage_pct, health='ok', created_at=None,
             origin='manual', ref_id=''):
    conn = sqlite3.connect(path)
    conn.execute('INSERT INTO backtest_runs (run_id, origin, ref_id, created_at) VALUES (?, ?, ?, ?)',
                 (run_id, origin, ref_id, created_at or _ts(hours_ago=1)))
    conn.execute('INSERT INTO backtest_results (run_id, coverage_pct, health) VALUES (?, ?, ?)',
                 (run_id, coverage_pct, health))
    conn.commit()
    conn.close()


def _add_task(path, task_id, origin='manual', ref_id='', status='completed', created_at=None):
    conn = sqlite3.connect(path)
    conn.execute('INSERT INTO data_tasks (task_id, origin, ref_id, status, created_at) VALUES (?, ?, ?, ?, ?)',
                 (task_id, origin, ref_id, status, created_at or _ts(hours_ago=1)))
    conn.commit()
    conn.close()


# ── 用例 1：空库 ──────────────────────────────────────────────

def test_empty_db_reports_zeros_and_hint(fake_db):
    r = bdb.get_backtest_readiness(30)
    assert r['days'] == 30
    assert r['runs'] == 0
    assert r['avg_coverage_pct'] is None
    assert r['gap_runs'] == 0
    assert r['gap_tasks'] == 0 and r['gap_tasks_done'] == 0 and r['refilled'] == 0
    assert r['hint'] and '近 30 天' in r['hint']
    assert '平均数据覆盖率 —' in r['hint']


# ── 用例 2：NULL 不进均值 ─────────────────────────────────────

def test_null_coverage_excluded_from_average(fake_db):
    _add_run(fake_db, 'BT-1', 0.90, 'ok', created_at=_ts(hours_ago=3))
    _add_run(fake_db, 'BT-2', None, 'ok', created_at=_ts(hours_ago=2))

    r = bdb.get_backtest_readiness(30)
    assert r['runs'] == 2
    assert r['avg_coverage_pct'] == 90.0   # AVG 忽略 NULL：分母只算 1 条
    assert r['gap_runs'] == 0


# ── 用例 3：缺口计数（health = 'data_gap'） ────────────────────

def test_gap_runs_counts_data_gap_health(fake_db):
    _add_run(fake_db, 'BT-A', 0.90, 'data_gap', created_at=_ts(hours_ago=3))
    _add_run(fake_db, 'BT-B', 0.949, 'data_gap', created_at=_ts(hours_ago=2))
    _add_run(fake_db, 'BT-C', 0.99, 'ok', created_at=_ts(hours_ago=1))

    r = bdb.get_backtest_readiness(30)
    assert r['runs'] == 3
    assert r['gap_runs'] == 2
    assert r['avg_coverage_pct'] == 94.6   # (0.90 + 0.949 + 0.99) / 3 × 100


# ── 用例 4：补数任务 + 补数后重跑 ─────────────────────────────

def test_gap_tasks_done_and_refilled(fake_db):
    _add_task(fake_db, 'DT-GAP-1', 'backtest_gap', 'BT-R1', 'running', created_at=_ts(days_ago=3))
    _add_task(fake_db, 'DT-GAP-2', 'backtest_gap', 'BT-R1', 'completed', created_at=_ts(days_ago=2))
    # 窗口外（40 天前）不应计入
    _add_task(fake_db, 'DT-GAP-OLD', 'backtest_gap', 'BT-R2', 'completed', created_at=_ts(days_ago=40))
    # 手工任务不算补数
    _add_task(fake_db, 'DT-MANUAL', 'manual', 'BT-R1', 'completed', created_at=_ts(days_ago=2))
    # 补数后重跑：同 ref_id、origin='manual'、created_at 晚于补数任务
    _add_run(fake_db, 'BT-R1', 0.98, 'ok', created_at=_ts(days_ago=1), origin='manual', ref_id='BT-R1')

    r = bdb.get_backtest_readiness(30)
    assert r['gap_tasks'] == 2          # 窗口内 backtest_gap 任务数（含未完成的）
    assert r['gap_tasks_done'] == 1     # 其中 status='completed' 的
    assert r['refilled'] == 1           # COUNT(DISTINCT ref_id)：补完且之后重跑过
    assert '补数后 1 次已重跑' in r['hint']

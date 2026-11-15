"""DB 层的**迁移未应用降级**单测。

背景：B 批给 ``backtest_runs`` / ``strategy_session`` 加了 ``exit_timing`` 列
（加性迁移，事实源 ``utils/db_schema/tables/backtest.py``）。生产库若还没跑迁移，
INSERT/UPDATE 里带这个列会直接 ``table has no column named exit_timing``，
把整条策略回测链打断 —— 这就是「加了一列就把功能搞挂」的最坏情况。

锁住契约：迁移没跑时**降级为不写这一列**，而不是抛错；status 等关键字段照常写入。
（跑完迁移重启进程即自动恢复 —— 探测结果按表在进程内缓存，不跨进程。）
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    from config import Config
    test_db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: test_db)
    from utils.cache import backtest_db as bd
    bd.init_backtest_tables()
    bd._MISSING_COLS.clear()
    yield test_db
    bd._MISSING_COLS.clear()


def _drop_column(db_path: str, table: str, column: str) -> None:
    """模拟「迁移还没跑」的库：把列去掉（SQLite 老版本无 DROP COLUMN）"""
    con = sqlite3.connect(db_path)
    cols = [r[1] for r in con.execute('PRAGMA table_info(%s)' % table)]
    ddl = [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))][0]
    body = ddl[ddl.index('(') + 1: ddl.rindex(')')]
    kept = [c.strip() for c in body.split(',') if c.strip().split()[0] != column]
    con.execute('PRAGMA writable_schema = ON')
    con.execute("UPDATE sqlite_master SET sql=? WHERE name=?", ('CREATE TABLE %s (%s)' % (table, ', '.join(kept)), table))
    con.commit()
    con.close()
    # 校验：列确实没了
    con = sqlite3.connect(db_path)
    assert column not in [r[1] for r in con.execute('PRAGMA table_info(%s)' % table)]
    assert column in cols
    con.close()


def test_session_create_survives_missing_exit_timing(tmp_backtest_db):
    from utils.cache.backtest_db import create_strategy_session, get_strategy_session
    _drop_column(tmp_backtest_db, 'strategy_session', 'exit_timing')

    row = create_strategy_session('ss-mig-1', 'local', '尾盘买入次日卖出',
                                 slots={'universe': '600519', 'entry_timing': 'same_close',
                                        'exit_timing': 'next_open'})
    assert row['status'] == 'pending_confirm'
    assert row['entry_timing'] == 'same_close'
    # exit_timing 落不进列，但必须留在 slots_json 里（迁移补上后可回溯）
    assert 'next_open' in (row.get('slots_json') or '')


def test_session_update_survives_missing_exit_timing(tmp_backtest_db):
    """UPDATE 时剔除缺失列，status 等关键字段不能被一起丢掉"""
    from utils.cache.backtest_db import (create_strategy_session, get_strategy_session,
                                         update_strategy_session)
    _drop_column(tmp_backtest_db, 'strategy_session', 'exit_timing')
    create_strategy_session('ss-mig-2', 'local', 'idea')

    update_strategy_session('ss-mig-2', status='active', exit_timing='next_close',
                            turn_count=3)
    row = get_strategy_session('ss-mig-2')
    assert row['status'] == 'active', '剔除缺失列时把 status 一起丢了'
    assert row['turn_count'] == 3


def test_run_create_survives_missing_exit_timing(tmp_backtest_db):
    from utils.cache.backtest_db import create_backtest_run
    _drop_column(tmp_backtest_db, 'backtest_runs', 'exit_timing')

    row = create_backtest_run('bt-mig-1', {
        'code': '600519', 'strategy_id': 'dual_ma_crossover',
        'start_date': '2025-01-01', 'end_date': '2025-12-31',
        'stock_name': '贵州茅台', 'entry_timing': 'same_close', 'exit_timing': 'next_close',
    })
    assert row['status'] == 'pending'
    assert row['entry_timing'] == 'same_close'


def test_columns_exist_after_migration(tmp_backtest_db):
    """迁移在时（跑过 init_backtest_tables 的正常库）两列都在，且能正常写入"""
    from utils.cache.backtest_db import (create_strategy_session, get_strategy_session,
                                         update_strategy_session)
    create_strategy_session('ss-mig-3', 'local', 'idea',
                            slots={'entry_timing': 'same_close', 'exit_timing': 'next_close'})
    row = get_strategy_session('ss-mig-3')
    assert row['exit_timing'] == 'next_close'
    update_strategy_session('ss-mig-3', exit_timing='same_close')
    assert get_strategy_session('ss-mig-3')['exit_timing'] == 'same_close'
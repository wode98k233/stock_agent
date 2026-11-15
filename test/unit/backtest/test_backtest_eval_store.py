"""backtest_eval 存取测试（Plan 3 任务 2）+ strategies.strategy_hash 写入口（任务 6 步骤 6.4）"""
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """临时库，绝不碰真库 db/backtest.db"""
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _sample(**over):
    data = {
        'is_metrics_json': '{"sharpe": 1.4, "trades": 30}',
        'oos_metrics_json': '{"sharpe": 1.0, "trades": 28}',
        'split_date': '2022-06-30',
        'is_sharpe': 1.4,
        'oos_sharpe': 1.0,
        'sharpe_ratio_io': 1.4,
        'overfit_flag': 0,
        'verdict': 'ok',
    }
    data.update(over)
    return data


def test_save_and_get_roundtrip(setup_test_db):
    """写入后读回，字段一一相等"""
    from utils.cache.backtest_db import save_backtest_eval, get_backtest_eval
    save_backtest_eval('BT-TEST-001', _sample())
    row = get_backtest_eval('BT-TEST-001')
    assert row is not None
    assert row['run_id'] == 'BT-TEST-001'
    assert row['is_sharpe'] == 1.4
    assert row['oos_sharpe'] == 1.0
    assert row['sharpe_ratio_io'] == 1.4
    assert row['overfit_flag'] == 0
    assert row['verdict'] == 'ok'
    assert row['split_date'] == '2022-06-30'
    assert row['created_at']
    assert row['updated_at']


def test_save_twice_keeps_one_row_and_last_wins(setup_test_db):
    """同一个 run_id 写两次：只有一行，第二次的值生效（INSERT OR REPLACE 语义）"""
    import sqlite3
    from utils.cache.backtest_db import save_backtest_eval, get_backtest_eval
    save_backtest_eval('BT-TEST-002', _sample())
    save_backtest_eval('BT-TEST-002', _sample(
        is_sharpe=2.0, oos_sharpe=0.5, sharpe_ratio_io=4.0,
        overfit_flag=1, verdict='overfit_suspect'))
    conn = sqlite3.connect(setup_test_db)
    count = conn.execute("SELECT COUNT(*) FROM backtest_eval WHERE run_id = 'BT-TEST-002'").fetchone()[0]
    conn.close()
    assert count == 1, '重复评判必须覆盖，不能堆行'
    row = get_backtest_eval('BT-TEST-002')
    assert row['verdict'] == 'overfit_suspect'
    assert row['overfit_flag'] == 1
    assert row['sharpe_ratio_io'] == 4.0
    assert row['is_sharpe'] == 2.0


def test_get_unknown_run_returns_none(setup_test_db):
    """没评判过返回 None（调用方据此显示「尚未评判」）"""
    from utils.cache.backtest_db import get_backtest_eval
    assert get_backtest_eval('BT-NOT-EVALUATED') is None


def test_unknown_keys_are_dropped_silently(setup_test_db):
    """白名单外的键被静默丢弃：不抛异常、不写列"""
    import sqlite3
    from utils.cache.backtest_db import save_backtest_eval, get_backtest_eval
    save_backtest_eval('BT-TEST-004', _sample(evil=1, run_id='HACKED'))
    conn = sqlite3.connect(setup_test_db)
    cols = [r[1] for r in conn.execute('PRAGMA table_info(backtest_eval)').fetchall()]
    conn.close()
    assert 'evil' not in cols
    row = get_backtest_eval('BT-TEST-004')
    assert row['run_id'] == 'BT-TEST-004', 'run_id 由参数决定，不能被 data 里同名键覆盖'


def test_empty_verdict_is_writable(setup_test_db):
    """verdict 为空字符串照样能写入（DEFAULT ''，不是 NOT NULL 约束冲突）"""
    from utils.cache.backtest_db import save_backtest_eval, get_backtest_eval
    save_backtest_eval('BT-TEST-005', {'verdict': '', 'is_sharpe': None, 'oos_sharpe': None})
    row = get_backtest_eval('BT-TEST-005')
    assert row['verdict'] == ''
    assert row['is_sharpe'] is None
    assert row['overfit_flag'] == 0, '建表默认值兜底'


def test_strategy_hash_column_and_update_helper(setup_test_db):
    """步骤 6.4：strategies.strategy_hash 列 + update_strategy_hash 回写（采纳后反查迭代史）"""
    from utils.cache.backtest_db import get_strategy, update_strategy_hash
    assert get_strategy('dual_ma_crossover')['strategy_hash'] == '', '新列默认空串'
    assert update_strategy_hash('dual_ma_crossover', 'abc123') is True
    assert get_strategy('dual_ma_crossover')['strategy_hash'] == 'abc123'
    assert update_strategy_hash('not-exist-strategy', 'abc123') is False

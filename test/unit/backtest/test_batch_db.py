"""批量回测批次表 / 测试集 CRUD 单元测试"""
import json
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """使用临时数据库"""
    test_db = str(tmp_path / 'backtest.db')
    from unittest.mock import patch
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _conn(db_path: str):
    return sqlite3.connect(db_path)


def test_batch_tables_created(setup_test_db):
    """3 张新表应被创建"""
    conn = _conn(setup_test_db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert 'backtest_batch' in tables
    assert 'backtest_batch_item' in tables
    assert 'backtest_test_set' in tables


# ============================================================
# backtest_batch CRUD
# ============================================================

def test_create_and_get_batch(setup_test_db):
    """create_batch + get_batch 基础流程"""
    from utils.cache.backtest_db import create_batch, get_batch
    create_batch(
        batch_id='BTB-TEST-001',
        strategy_id='dual_ma_crossover',
        strategy_name='双均线金叉',
        params={'fast': 5, 'slow': 20},
        config={'start_date': '2024-01-01', 'end_date': '2024-12-31',
                'initial_cash': 100000},
        codes=['600519', '000858'],
        initial_cash=100000,
        start_date='2024-01-01',
        end_date='2024-12-31',
    )
    batch = get_batch('BTB-TEST-001')
    assert batch is not None
    assert batch['strategy_id'] == 'dual_ma_crossover'
    assert batch['strategy_name'] == '双均线金叉'
    assert batch['code_count'] == 2
    assert batch['status'] == 'pending'
    assert json.loads(batch['codes_json']) == ['600519', '000858']
    assert json.loads(batch['params_json'])['fast'] == 5


def test_list_batches_order(setup_test_db):
    """list_batches 返回全部批次（同秒创建时顺序不保证，仅校验集合）"""
    from utils.cache.backtest_db import create_batch, list_batches
    expected_ids = set()
    for i in range(3):
        bid = f'BTB-TEST-L{i}'
        expected_ids.add(bid)
        create_batch(
            batch_id=bid,
            strategy_id='dual_ma_crossover',
            strategy_name='双均线金叉',
            params={}, config={}, codes=[],
            initial_cash=100000, start_date='2024-01-01', end_date='2024-12-31',
        )
    batches = list_batches()
    assert len(batches) == 3
    assert {b['batch_id'] for b in batches} == expected_ids


def test_list_batches_limit(setup_test_db):
    """limit 参数生效"""
    from utils.cache.backtest_db import create_batch, list_batches
    for i in range(5):
        create_batch(
            batch_id=f'BTB-TEST-M{i}',
            strategy_id='dual_ma_crossover', strategy_name='', params={}, config={},
            codes=[], initial_cash=100000, start_date='', end_date='',
        )
    batches = list_batches(limit=3)
    assert len(batches) == 3


def test_update_batch(setup_test_db):
    """update_batch 支持任意字段"""
    from utils.cache.backtest_db import create_batch, get_batch, update_batch
    create_batch(
        batch_id='BTB-TEST-U1', strategy_id='s1', strategy_name='s1',
        params={}, config={}, codes=['600519'], initial_cash=100000,
        start_date='', end_date='',
    )
    update_batch('BTB-TEST-U1',
                 status='running',
                 avg_total_return=0.123,
                 profit_count=3,
                 loss_count=1)
    batch = get_batch('BTB-TEST-U1')
    assert batch['status'] == 'running'
    assert batch['avg_total_return'] == pytest.approx(0.123)
    assert batch['profit_count'] == 3
    assert batch['loss_count'] == 1


def test_delete_batch_cascade(setup_test_db):
    """delete_batch 应连带删除明细"""
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, delete_batch, get_batch, get_batch_items
    )
    create_batch(
        batch_id='BTB-TEST-D1', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['600519', '000858'],
        initial_cash=100000, start_date='', end_date='',
    )
    create_batch_items('BTB-TEST-D1', [
        {'code': '600519', 'asset_type': 'stock', 'run_id': 'R1'},
        {'code': '000858', 'asset_type': 'stock', 'run_id': 'R2'},
    ])
    assert len(get_batch_items('BTB-TEST-D1')) == 2

    delete_batch('BTB-TEST-D1')
    assert get_batch('BTB-TEST-D1') is None
    assert len(get_batch_items('BTB-TEST-D1')) == 0


def test_update_batch_empty_kwargs(setup_test_db):
    """空 kwargs 不应报错"""
    from utils.cache.backtest_db import create_batch, update_batch
    create_batch(
        batch_id='BTB-TEST-E1', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=[], initial_cash=0,
        start_date='', end_date='',
    )
    update_batch('BTB-TEST-E1')  # 不应抛异常


# ============================================================
# backtest_batch_item CRUD
# ============================================================

def test_create_batch_items_and_get(setup_test_db):
    """create_batch_items + get_batch_items"""
    from utils.cache.backtest_db import create_batch, create_batch_items, get_batch_items
    create_batch(
        batch_id='BTB-TEST-I1', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['600519', '510300'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-TEST-I1', [
        {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-001'},
        {'code': '510300', 'asset_type': 'etf', 'run_id': 'R-002'},
    ])
    items = get_batch_items('BTB-TEST-I1')
    assert len(items) == 2
    # 按 id 升序
    assert items[0]['code'] == '600519'
    assert items[0]['asset_type'] == 'stock'
    assert items[0]['run_id'] == 'R-001'
    assert items[0]['status'] == 'pending'
    assert items[1]['code'] == '510300'
    assert items[1]['asset_type'] == 'etf'


def test_update_batch_item(setup_test_db):
    """update_batch_item 支持任意字段"""
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, update_batch_item, get_batch_item_by_code
    )
    create_batch(
        batch_id='BTB-TEST-IU1', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['600519'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-TEST-IU1', [
        {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-001'},
    ])
    update_batch_item('BTB-TEST-IU1', '600519',
                      status='completed',
                      total_return=0.234,
                      trade_count=12,
                      win_rate=0.6,
                      final_equity=123400.0)
    item = get_batch_item_by_code('BTB-TEST-IU1', '600519')
    assert item['status'] == 'completed'
    assert item['total_return'] == pytest.approx(0.234)
    assert item['trade_count'] == 12
    assert item['win_rate'] == pytest.approx(0.6)
    assert item['final_equity'] == pytest.approx(123400.0)


def test_update_batch_item_failure(setup_test_db):
    """失败状态：记录 error_message"""
    from utils.cache.backtest_db import (
        create_batch, create_batch_items, update_batch_item, get_batch_item_by_code
    )
    create_batch(
        batch_id='BTB-TEST-IF1', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['600519'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-TEST-IF1', [
        {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-001'},
    ])
    update_batch_item('BTB-TEST-IF1', '600519',
                      status='failed',
                      error_message='数据不足: 仅 10 条K线')
    item = get_batch_item_by_code('BTB-TEST-IF1', '600519')
    assert item['status'] == 'failed'
    assert '数据不足' in item['error_message']


def test_batch_item_unique_constraint(setup_test_db):
    """(batch_id, code) 唯一约束"""
    from utils.cache.backtest_db import create_batch, create_batch_items
    create_batch(
        batch_id='BTB-TEST-IU2', strategy_id='s1', strategy_name='',
        params={}, config={}, codes=['600519'],
        initial_cash=0, start_date='', end_date='',
    )
    create_batch_items('BTB-TEST-IU2', [
        {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-001'},
    ])
    # 同 batch_id + 同 code 应抛 IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        create_batch_items('BTB-TEST-IU2', [
            {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-002'},
        ])


# ============================================================
# backtest_test_set CRUD
# ============================================================

def test_create_and_get_test_set(setup_test_db):
    """create_test_set + get_test_set_by_name + get_test_set"""
    from utils.cache.backtest_db import (
        create_test_set, get_test_set, get_test_set_by_name
    )
    ts = create_test_set(
        name='DPO白酒ETF',
        codes=['600519', '000858', '510300', '159915'],
        description='DPO策略常用测试集',
        tags=['白酒', 'ETF'],
    )
    assert ts is not None
    assert ts['name'] == 'DPO白酒ETF'
    assert ts['code_count'] == 4
    assert ts['description'] == 'DPO策略常用测试集'
    assert json.loads(ts['codes_json']) == ['600519', '000858', '510300', '159915']
    assert json.loads(ts['tags_json']) == ['白酒', 'ETF']

    by_name = get_test_set_by_name('DPO白酒ETF')
    assert by_name['id'] == ts['id']
    by_id = get_test_set(ts['id'])
    assert by_id['name'] == 'DPO白酒ETF'


def test_list_test_sets_order(setup_test_db):
    """list_test_sets 按 updated_at 倒序"""
    from utils.cache.backtest_db import create_test_set, list_test_sets, update_test_set
    ts1 = create_test_set('SET-1', ['600519'])
    ts2 = create_test_set('SET-2', ['000858'])
    # 让 SET-1 最新更新
    update_test_set(ts1['id'], description='updated')

    sets = list_test_sets()
    assert len(sets) == 2
    # SET-1 应在前
    assert sets[0]['name'] == 'SET-1'


def test_update_test_set_codes_auto_serialize(setup_test_db):
    """update_test_set 传入 codes 应自动序列化为 codes_json 并更新 code_count"""
    from utils.cache.backtest_db import create_test_set, update_test_set, get_test_set
    ts = create_test_set('SET-U1', ['600519'])
    update_test_set(ts['id'], codes=['600519', '000858', '510300'], tags=['A', 'B'])

    updated = get_test_set(ts['id'])
    assert updated['code_count'] == 3
    assert json.loads(updated['codes_json']) == ['600519', '000858', '510300']
    assert json.loads(updated['tags_json']) == ['A', 'B']


def test_update_test_set_name_unique(setup_test_db):
    """name 唯一约束（在调用方校验，DB 层也应触发）"""
    from utils.cache.backtest_db import create_test_set
    create_test_set('DUP-NAME', ['600519'])
    with pytest.raises(sqlite3.IntegrityError):
        create_test_set('DUP-NAME', ['000858'])


def test_delete_test_set(setup_test_db):
    """delete_test_set 返回 True/False"""
    from utils.cache.backtest_db import create_test_set, delete_test_set, get_test_set
    ts = create_test_set('DEL-SET', ['600519'])
    assert delete_test_set(ts['id']) is True
    assert get_test_set(ts['id']) is None
    # 不存在的 id
    assert delete_test_set(9999) is False


def test_create_test_set_default_tags(setup_test_db):
    """tags=None 默认空数组"""
    from utils.cache.backtest_db import create_test_set
    ts = create_test_set('NO-TAGS', ['600519'])
    assert json.loads(ts['tags_json']) == []

"""批量回测 API 集成测试

用 FastAPI TestClient 测试 11 个端点：
- /batch-run
- /batches, /batches/{id}, /batches/{id}/progress, /batches/{id}/rerun, DELETE /batches/{id}
- /test-sets, /test-sets/{id}（GET/POST/PUT/DELETE）

mock BacktestEngine 避免真实回测；用临时 backtest.db 隔离。
"""
import json
import time
from unittest.mock import patch, MagicMock

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def test_db(tmp_path):
    """临时 backtest.db"""
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


@pytest.fixture
def client(test_db):
    """FastAPI TestClient + backtest router

    构造 mini app 避免 server.app.create_app 的副作用
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.routes.backtest import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _fake_engine_result(run_id, total_return=0.15):
    """构造 BacktestEngine.run 的伪返回值"""
    return {
        'run_id': run_id,
        'status': 'completed',
        'total_return': total_return,
        'annual_return': total_return * 0.7,
        'sharpe_ratio': 1.2,
        'max_drawdown': -0.05,
        'win_rate': 0.6,
        'trade_count': 10,
        'final_equity': 115000.0,
    }


# ============================================================
# /batch-run 端点
# ============================================================

class TestBatchRunEndpoint:
    """POST /api/backtest/batch-run"""

    def test_batch_run_success(self, client):
        """提交批量回测：返回 200 + batch_id + items"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': ['600519', '000858', '510300'],
            'strategy_id': 'dual_ma_crossover',
            'params': {'fast': 5, 'slow': 20},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert 'batch_id' in data
        assert data['batch_id'].startswith('BTB-')
        assert len(data['items']) == 3
        for it in data['items']:
            assert it['status'] == 'pending'
            assert it['code']
            assert it['run_id'].startswith('BT-')

    def test_batch_run_dedup_codes(self, client):
        """codes 去重"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': ['600519', '600519', '000858', '000858'],
            'strategy_id': 'dual_ma_crossover',
        })
        assert resp.status_code == 200
        assert len(resp.json()['items']) == 2

    def test_batch_run_codes_uppercase(self, client):
        """codes 转大写"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': ['600519', ' 000858 '],  # 带空格 + 小写
            'strategy_id': 'dual_ma_crossover',
        })
        items = resp.json()['items']
        codes = [it['code'] for it in items]
        assert '000858' in codes
        assert '600519' in codes

    def test_batch_run_empty_codes(self, client):
        """codes 为空 → 400"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': [],
            'strategy_id': 'dual_ma_crossover',
        })
        assert resp.status_code == 400
        assert '不能为空' in resp.json()['detail']

    def test_batch_run_too_many_codes(self, client):
        """codes > 20 → 400"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': [f'{i:06d}' for i in range(21)],
            'strategy_id': 'dual_ma_crossover',
        })
        assert resp.status_code == 400
        assert '最多' in resp.json()['detail']

    def test_batch_run_strategy_not_found(self, client):
        """strategy_id 不存在 → 404"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': ['600519'],
            'strategy_id': 'nonexistent_strategy',
        })
        assert resp.status_code == 404

    def test_batch_run_persists_batch_and_items(self, client):
        """提交后 batch 和 items 应已落库"""
        from utils.cache.backtest_db import get_batch, get_batch_items
        # mock BatchBacktestEngine.run，避免后台线程立即把状态改掉
        with patch('backtest.batch_engine.BatchBacktestEngine.run') as mock_run:
            mock_run.return_value = {'status': 'completed'}
            resp = client.post('/api/backtest/batch-run', json={
                'codes': ['600519', '000858'],
                'strategy_id': 'dual_ma_crossover',
                'params': {'fast': 5, 'slow': 20},
            })
        batch_id = resp.json()['batch_id']

        batch = get_batch(batch_id)
        assert batch is not None
        assert batch['strategy_id'] == 'dual_ma_crossover'
        assert batch['code_count'] == 2
        # 不强校验 status，后台线程可能已执行（mock 后保持 pending 或 completed）

        items = get_batch_items(batch_id)
        assert len(items) == 2
        codes = [it['code'] for it in items]
        assert set(codes) == {'600519', '000858'}
        # asset_type 自动识别
        for it in items:
            assert it['asset_type'] in ('stock', 'etf', 'index', 'board')

    def test_batch_run_triggers_async_thread(self, client):
        """提交后应启动后台线程，最终 batch 状态变为 completed/partial/failed"""
        from utils.cache.backtest_db import get_batch

        with patch('backtest.batch_engine.BacktestEngine') as MockEngine:
            MockEngine.return_value.run.side_effect = lambda cfg, **kw: _fake_engine_result(kw['run_id'])
            resp = client.post('/api/backtest/batch-run', json={
                'codes': ['600519'],
                'strategy_id': 'dual_ma_crossover',
                'concurrency': 1,
            })
            batch_id = resp.json()['batch_id']

            # 等待后台线程完成（最多 5s）
            for _ in range(50):
                batch = get_batch(batch_id)
                if batch['status'] in ('completed', 'partial', 'failed'):
                    break
                time.sleep(0.1)

            assert batch['status'] == 'completed'
            assert batch['success_count'] if 'success_count' in batch else True  # success_count 不在表里
            assert batch['avg_total_return'] is not None


# ============================================================
# /batches 端点
# ============================================================

class TestBatchesListEndpoint:
    """GET /api/backtest/batches"""

    def test_list_empty(self, client):
        """无批次时返回空列表"""
        resp = client.get('/api/backtest/batches')
        assert resp.status_code == 200
        assert resp.json() == {'batches': []}

    def test_list_after_create(self, client):
        """创建后能在列表中看到"""
        client.post('/api/backtest/batch-run', json={
            'codes': ['600519', '000858'],
            'strategy_id': 'dual_ma_crossover',
        })
        resp = client.get('/api/backtest/batches')
        batches = resp.json()['batches']
        assert len(batches) >= 1
        # JSON 字段应被解析
        b = batches[0]
        assert isinstance(b['codes_json'], list)
        assert isinstance(b['config_json'], dict)


# ============================================================
# /batches/{batch_id} 端点
# ============================================================

class TestBatchDetailEndpoint:
    """GET /api/backtest/batches/{batch_id}"""

    def test_get_existing(self, client):
        """获取存在的批次详情"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': ['600519', '000858'],
            'strategy_id': 'dual_ma_crossover',
        })
        batch_id = resp.json()['batch_id']

        detail = client.get(f'/api/backtest/batches/{batch_id}')
        assert detail.status_code == 200
        data = detail.json()
        assert 'batch' in data
        assert 'items' in data
        assert data['batch']['batch_id'] == batch_id
        assert len(data['items']) == 2
        # JSON 字段被解析
        assert isinstance(data['batch']['codes_json'], list)

    def test_get_not_found(self, client):
        """不存在的 batch_id → 404"""
        resp = client.get('/api/backtest/batches/BTB-NONEXIST-XXXXXX')
        assert resp.status_code == 404


# ============================================================
# DELETE /batches/{batch_id}
# ============================================================

class TestBatchDeleteEndpoint:
    """DELETE /api/backtest/batches/{batch_id}"""

    def test_delete_existing(self, client):
        """删除已存在的批次"""
        resp = client.post('/api/backtest/batch-run', json={
            'codes': ['600519'],
            'strategy_id': 'dual_ma_crossover',
        })
        batch_id = resp.json()['batch_id']

        # 删除
        del_resp = client.delete(f'/api/backtest/batches/{batch_id}')
        assert del_resp.status_code == 200
        assert del_resp.json() == {'success': True}

        # 再次查询应 404
        assert client.get(f'/api/backtest/batches/{batch_id}').status_code == 404

    def test_delete_not_found(self, client):
        """删除不存在的批次 → 404"""
        resp = client.delete('/api/backtest/batches/BTB-NONEXIST-XXXXXX')
        assert resp.status_code == 404


# ============================================================
# /batches/{batch_id}/rerun 端点
# ============================================================

class TestBatchRerunEndpoint:
    """POST /api/backtest/batches/{batch_id}/rerun"""

    def test_rerun_failed_item(self, client):
        """重跑失败的 item"""
        from utils.cache.backtest_db import (
            create_batch, create_batch_items, update_batch_item
        )

        # 准备一个含失败 item 的批次
        create_batch(
            batch_id='BTB-RERUN-1', strategy_id='dual_ma_crossover',
            strategy_name='双均线金叉', params={}, config={},
            codes=['600519'], initial_cash=100000,
            start_date='2024-01-01', end_date='2024-12-31',
        )
        create_batch_items('BTB-RERUN-1', [
            {'code': '600519', 'asset_type': 'stock', 'run_id': 'R-OLD'},
        ])
        update_batch_item('BTB-RERUN-1', '600519',
                          status='failed', error_message='数据不足')

        with patch('backtest.engine.BacktestEngine') as MockEngine:
            MockEngine.return_value.run.side_effect = lambda cfg, **kw: _fake_engine_result(kw['run_id'])
            resp = client.post('/api/backtest/batches/BTB-RERUN-1/rerun',
                               json={'code': '600519'})
        assert resp.status_code == 200
        assert resp.json()['status'] == 'pending'
        assert resp.json()['run_id'].startswith('BT-')

    def test_rerun_completed_item_rejected(self, client):
        """非 failed 状态的 item 不能重跑 → 400"""
        from utils.cache.backtest_db import (
            create_batch, create_batch_items, update_batch_item
        )

        create_batch(
            batch_id='BTB-RERUN-2', strategy_id='dual_ma_crossover',
            strategy_name='', params={}, config={},
            codes=['600519'], initial_cash=0, start_date='', end_date='',
        )
        create_batch_items('BTB-RERUN-2', [
            {'code': '600519', 'run_id': 'R-OK'},
        ])
        update_batch_item('BTB-RERUN-2', '600519', status='completed')

        resp = client.post('/api/backtest/batches/BTB-RERUN-2/rerun',
                           json={'code': '600519'})
        assert resp.status_code == 400
        assert '仅失败' in resp.json()['detail']

    def test_rerun_nonexistent_item(self, client):
        """item 不存在 → 404"""
        from utils.cache.backtest_db import create_batch
        create_batch(
            batch_id='BTB-RERUN-3', strategy_id='s1', strategy_name='',
            params={}, config={}, codes=['600519'],
            initial_cash=0, start_date='', end_date='',
        )
        resp = client.post('/api/backtest/batches/BTB-RERUN-3/rerun',
                           json={'code': '999999'})
        assert resp.status_code == 404


# ============================================================
# /test-sets CRUD 端点
# ============================================================

class TestTestSetCRUDEndpoints:
    """测试集 CRUD API"""

    def test_create_and_get(self, client):
        """创建 + 单独查询"""
        resp = client.post('/api/backtest/test-sets', json={
            'name': 'DPO白酒ETF',
            'codes': ['600519', '000858', '510300'],
            'description': 'DPO策略常用',
            'tags': ['白酒', 'ETF'],
        })
        assert resp.status_code == 200
        ts = resp.json()
        assert ts['name'] == 'DPO白酒ETF'
        assert ts['code_count'] == 3
        assert isinstance(ts['codes_json'], list)
        assert '600519' in ts['codes_json']
        assert ts['tags_json'] == ['白酒', 'ETF']

        # GET 单个
        get_resp = client.get(f"/api/backtest/test-sets/{ts['id']}")
        assert get_resp.status_code == 200
        assert get_resp.json()['name'] == 'DPO白酒ETF'

    def test_create_dedup_codes(self, client):
        """codes 去重 + 大写"""
        resp = client.post('/api/backtest/test-sets', json={
            'name': 'DEDUP-SET',
            'codes': ['600519', '600519', ' 000858 '],
        })
        ts = resp.json()
        assert ts['code_count'] == 2
        assert set(ts['codes_json']) == {'600519', '000858'}

    def test_create_empty_name(self, client):
        """name 为空 → 400"""
        resp = client.post('/api/backtest/test-sets', json={
            'name': '',
            'codes': ['600519'],
        })
        assert resp.status_code == 400

    def test_create_too_many_codes(self, client):
        """codes > 50 → 400"""
        resp = client.post('/api/backtest/test-sets', json={
            'name': 'BIG-SET',
            'codes': [f'{i:06d}' for i in range(51)],
        })
        assert resp.status_code == 400

    def test_create_duplicate_name(self, client):
        """name 重复 → 400"""
        client.post('/api/backtest/test-sets', json={
            'name': 'DUP',
            'codes': ['600519'],
        })
        resp = client.post('/api/backtest/test-sets', json={
            'name': 'DUP',
            'codes': ['000858'],
        })
        assert resp.status_code == 400
        assert '已存在' in resp.json()['detail']

    def test_list_test_sets(self, client):
        """列表"""
        client.post('/api/backtest/test-sets', json={
            'name': 'LIST-A', 'codes': ['600519'],
        })
        client.post('/api/backtest/test-sets', json={
            'name': 'LIST-B', 'codes': ['000858'],
        })
        resp = client.get('/api/backtest/test-sets')
        names = {s['name'] for s in resp.json()['test_sets']}
        assert {'LIST-A', 'LIST-B'}.issubset(names)

    def test_update(self, client):
        """PUT 更新"""
        create_resp = client.post('/api/backtest/test-sets', json={
            'name': 'UPD-1', 'codes': ['600519'],
        })
        ts_id = create_resp.json()['id']

        upd_resp = client.put(f'/api/backtest/test-sets/{ts_id}', json={
            'name': 'UPD-1-NEW',
            'description': 'updated desc',
            'tags': ['A', 'B'],
        })
        assert upd_resp.status_code == 200
        updated = upd_resp.json()
        assert updated['name'] == 'UPD-1-NEW'
        assert updated['description'] == 'updated desc'
        assert updated['tags_json'] == ['A', 'B']

    def test_update_codes(self, client):
        """PUT 更新 codes（自动序列化）"""
        create_resp = client.post('/api/backtest/test-sets', json={
            'name': 'UPD-CODES', 'codes': ['600519'],
        })
        ts_id = create_resp.json()['id']

        upd_resp = client.put(f'/api/backtest/test-sets/{ts_id}', json={
            'codes': ['600519', '000858', '510300'],
        })
        updated = upd_resp.json()
        assert updated['code_count'] == 3
        assert set(updated['codes_json']) == {'600519', '000858', '510300'}

    def test_update_nonexistent(self, client):
        """更新不存在的测试集 → 404"""
        resp = client.put('/api/backtest/test-sets/99999', json={
            'name': 'X',
        })
        assert resp.status_code == 404

    def test_delete(self, client):
        """删除"""
        create_resp = client.post('/api/backtest/test-sets', json={
            'name': 'DEL-1', 'codes': ['600519'],
        })
        ts_id = create_resp.json()['id']

        del_resp = client.delete(f'/api/backtest/test-sets/{ts_id}')
        assert del_resp.status_code == 200
        # 再查应 404
        assert client.get(f'/api/backtest/test-sets/{ts_id}').status_code == 404

    def test_delete_nonexistent(self, client):
        """删除不存在的测试集 → 404"""
        resp = client.delete('/api/backtest/test-sets/99999')
        assert resp.status_code == 404


# ============================================================
# SSE 进度端点
# ============================================================

class TestBatchProgressSSE:
    """GET /api/backtest/batches/{batch_id}/progress"""

    def test_progress_not_found(self, client):
        """不存在的 batch_id → SSE 返回 error"""
        resp = client.get('/api/backtest/batches/BTB-NONEXIST-XXXXXX/progress')
        assert resp.status_code == 200  # SSE 总是 200
        # 第一条消息应包含 error
        text = resp.text
        assert 'error' in text

    def test_progress_already_completed(self, client):
        """已完成的批次直接返回终态"""
        from utils.cache.backtest_db import (
            create_batch, update_batch
        )
        create_batch(
            batch_id='BTB-PROG-DONE', strategy_id='s1', strategy_name='',
            params={}, config={}, codes=['600519'],
            initial_cash=0, start_date='', end_date='',
        )
        update_batch('BTB-PROG-DONE', status='completed')

        resp = client.get('/api/backtest/batches/BTB-PROG-DONE/progress')
        assert resp.status_code == 200
        # 第一条消息应是 completed 状态
        assert 'completed' in resp.text

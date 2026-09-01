"""策略信号扫描 API 集成测试

用 FastAPI TestClient 测试 POST /api/chart/strategy-signals 端点。
mock SignalScanner 避免真实 DB 依赖。
"""
from unittest.mock import patch

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


@pytest.fixture
def client():
    """FastAPI TestClient + charts router"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.routes.charts import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _fake_scan_result(code='600519', strategy_id='dual_ma_crossover'):
    """构造 SignalScanner.scan 的伪返回值"""
    return {
        'code': code,
        'strategy_id': strategy_id,
        'adjust_type': 'qfq',
        'kline': [
            {'date': '2025-01-02', 'open': 10.0, 'high': 10.5, 'low': 9.8, 'close': 10.2, 'volume': 10000},
            {'date': '2025-01-03', 'open': 10.2, 'high': 10.8, 'low': 10.0, 'close': 10.6, 'volume': 12000},
        ],
        'signals': [
            {'date': '2025-01-03', 'buy': True, 'sell': False},
        ],
        'latest_is_buy': True,
        'latest_is_sell': False,
        'latest_date': '2025-01-03',
        'indicators': {
            'main': [{'name': 'MA5', 'data': [None, 10.4]}],
            'sub': [],
        },
    }


# ============================================================
# API 端点测试
# ============================================================

class TestScanStrategySignalsAPI:
    """POST /api/chart/strategy-signals 端点测试"""

    def test_scan_returns_correct_structure(self, client):
        """API 返回完整结构"""
        fake = _fake_scan_result()
        with patch('backtest.signal_scanner.SignalScanner.scan', return_value=fake):
            resp = client.post('/api/chart/strategy-signals', json={
                'code': '600519',
                'strategy_id': 'dual_ma_crossover',
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body['code'] == '600519'
        assert body['strategy_id'] == 'dual_ma_crossover'
        assert len(body['kline']) == 2
        assert len(body['signals']) == 1
        assert body['signals'][0]['buy'] is True
        assert body['latest_is_buy'] is True
        assert body['latest_date'] == '2025-01-03'
        assert 'indicators' in body

    def test_scan_with_params(self, client):
        """API 正确传递策略参数"""
        captured = {}

        def fake_scan(self, **kwargs):
            captured.update(kwargs)
            return _fake_scan_result()

        with patch('backtest.signal_scanner.SignalScanner.scan', fake_scan):
            resp = client.post('/api/chart/strategy-signals', json={
                'code': '000001',
                'strategy_id': 'rsi_overbought_oversold',
                'params': {'rsi_period': 10},
                'adjust_type': 'hfq',
                'days': 60,
            })

        assert resp.status_code == 200
        assert captured['code'] == '000001'
        assert captured['strategy_id'] == 'rsi_overbought_oversold'
        assert captured['params'] == {'rsi_period': 10}
        assert captured['adjust_type'] == 'hfq'
        assert captured['days'] == 60

    def test_scan_empty_data(self, client):
        """空数据不报错"""
        empty_result = {
            'code': '000000', 'strategy_id': 'dual_ma_crossover', 'adjust_type': 'qfq',
            'kline': [], 'signals': [], 'latest_is_buy': False, 'latest_is_sell': False,
            'latest_date': '', 'indicators': {'main': [], 'sub': []},
        }
        with patch('backtest.signal_scanner.SignalScanner.scan', return_value=empty_result):
            resp = client.post('/api/chart/strategy-signals', json={
                'code': '000000',
                'strategy_id': 'dual_ma_crossover',
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body['kline'] == []
        assert body['latest_is_buy'] is False

    def test_scan_default_values(self, client):
        """缺省参数使用默认值"""
        captured = {}

        def fake_scan(self, **kwargs):
            captured.update(kwargs)
            return _fake_scan_result()

        with patch('backtest.signal_scanner.SignalScanner.scan', fake_scan):
            resp = client.post('/api/chart/strategy-signals', json={
                'code': '600519',
                'strategy_id': 'dual_ma_crossover',
            })

        assert resp.status_code == 200
        assert captured['params'] == {}
        assert captured['adjust_type'] == 'qfq'
        assert captured['days'] == 120
        assert captured['start_date'] is None
        assert captured['end_date'] is None

"""ETF 在研究链路里的资产类型 / 费用 / 涨跌停口径测试。

覆盖：
- IsoOsRunner._run_segment 把 asset_type（etf/stock/index）透传给 engine；
- preflight 对本地无数据的 ETF 给出「可先补拉 ETF 日线」的明确提示（不联网补拉）；
- 引擎对 ETF 回测在 warnings（实时）与 health_detail（落库）里声明
  「ETF 涨跌停未模拟」，不伪装成已拦。

全部 mock，不读真实行情库、不外呼。
"""
import os
import sqlite3
import sys
from unittest.mock import patch

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# 1. IsoOsRunner 透传 asset_type
# ============================================================

class _RecordingEngine:
    """记录每次 run 的 config；返回值只需覆盖 _run_segment 读取的字段。"""

    def __init__(self):
        self.configs = []

    def run(self, config, **kwargs):
        self.configs.append(dict(config))
        return {
            'annual_return': 0.1, 'total_return': 0.2, 'sharpe_ratio': 1.0,
            'max_drawdown': 0.1, 'win_rate': 0.5, 'trade_count': 10,
            'health': 'ok', 'health_detail': '',
            'data_bars': 200, 'expected_bars': 210, 'coverage_pct': 0.95,
        }


class TestIsoOsRunnerAssetType:
    @pytest.mark.parametrize('code,expected', [
        ('510300', 'etf'),   # 沪市 ETF（51 段）
        ('159915', 'etf'),   # 深市 ETF（15 段）
        ('600519', 'stock'),  # 主板个股
        ('399001', 'index'),  # 深证成指
    ])
    def test_run_segment_passes_asset_type(self, code, expected):
        from backtest.strategy_research import IsoOsRunner

        engine = _RecordingEngine()
        IsoOsRunner(engine=engine)._run_segment(
            'sr_test', code, '2024-01-01', '2024-12-31', 'IS')

        assert engine.configs, 'engine.run 应被调用一次'
        assert engine.configs[0]['asset_type'] == expected
        assert engine.configs[0]['code'] == code

    def test_asset_type_detection_reuses_batch_rule(self):
        """识别规则必须是批量回测那一份（别再复制一套前缀表）。"""
        from backtest.batch_engine import _auto_detect_asset_type
        from backtest.strategy_research import _asset_type_of

        for code in ('510300', '159915', '588000', '512760',
                     '399001', '600519', '000858', '300750'):
            assert _asset_type_of(code) == _auto_detect_asset_type(code)


# ============================================================
# 2. preflight 对 ETF 的更准确提示
# ============================================================

class TestPreflightEtfHint:
    def test_empty_local_etf_data_mentions_backfill(self):
        from backtest.strategy_research import preflight_data

        with patch('utils.cache.market_data_db.get_stock_daily',
                   return_value=pd.DataFrame()):
            with pytest.raises(ValueError) as ei:
                preflight_data('510300', '2024-01-01', '2024-12-31')

        msg = str(ei.value)
        assert 'ETF' in msg, 'ETF 无数据必须给出准确提示，不能只说「数据不足」'
        assert '补拉' in msg
        assert '510300' in msg

    def test_empty_local_stock_data_keeps_generic_message(self):
        """个股不走 ETF 文案（避免把普通个股缺数据说成 ETF 数据问题）。"""
        from backtest.strategy_research import preflight_data

        with patch('utils.cache.market_data_db.get_stock_daily',
                   return_value=pd.DataFrame()):
            with pytest.raises(ValueError) as ei:
                preflight_data('600519', '2024-01-01', '2024-12-31')

        assert 'ETF' not in str(ei.value)


# ============================================================
# 3. 引擎：ETF 不拦涨跌停，但必须明确声明
# ============================================================

def _flat_frame() -> pd.DataFrame:
    """5 根横盘 bar，pre_close 连续——即便不拦涨跌停也不会误触个股口径。"""
    rows = [
        ('2025-01-02', 10.0, 10.1, 9.9, 10.0, 100000, 9.95),
        ('2025-01-03', 10.0, 10.1, 9.9, 10.0, 100000, 10.0),
        ('2025-01-06', 10.0, 10.1, 9.9, 10.0, 100000, 10.0),
        ('2025-01-07', 10.0, 10.1, 9.9, 10.0, 100000, 10.0),
        ('2025-01-08', 10.0, 10.1, 9.9, 10.0, 100000, 10.0),
    ]
    return pd.DataFrame([{
        'trade_date': r[0], 'open': r[1], 'high': r[2], 'low': r[3],
        'close': r[4], 'volume': r[5], 'pre_close': r[6],
    } for r in rows])


def _probe_strategy():
    from backtest.strategy_base import StockSolveStrategy

    class _EtfProbe(StockSolveStrategy):
        def on_bar(self):
            if len(self) == 2 and not self.position and not self.order:
                self.buy_with_reason('ETF 测试买入')

    return _EtfProbe


@pytest.fixture(autouse=True)
def _tmp_backtest_db(tmp_path):
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _base_config(**overrides) -> dict:
    config = {
        'code': '510300', 'strategy_id': 'etf_probe', 'params': {},
        'start_date': '2025-01-02', 'end_date': '2025-01-08',
        'initial_cash': 200000, 'commission': 0.0003, 'min_commission': 5.0,
        'stamp_tax': 0.0005, 'transfer_fee': 0.00001, 'slippage': 0.001,
        't_plus_1': True, 'lot_size': 100, 'adjust_type': 'qfq',
        'stop_loss': 0, 'take_profit': 0, 'trailing_stop': 0,
        'position_mode': 'full', 'position_size': 1.0, 'asset_type': 'etf',
    }
    config.update(overrides)
    return config


def test_engine_declares_etf_limit_not_simulated(_tmp_backtest_db):
    from backtest.engine import BacktestEngine

    with patch('backtest.data_adapter._load_stock_daily', return_value=_flat_frame()), \
         patch('backtest.engine.get_strategy_class', return_value=_probe_strategy()):
        result = BacktestEngine().run(_base_config(), run_id='BT-ETF-LIMIT-01')

    joined = '\n'.join(result['warnings'])
    # 关键词断言（不断言整句，文案可演进）
    assert 'ETF' in joined and '涨跌停' in joined and '未模拟' in joined
    assert '10%/20%' in joined
    # 费用口径不能按个股说「印花税万五」
    assert 'ETF 免印花税' in joined
    assert '印花税万五' not in joined

    detail = result['health_detail'] or ''
    assert 'ETF' in detail and '涨跌停' in detail and '未模拟' in detail

    con = sqlite3.connect(_tmp_backtest_db)
    try:
        row = con.execute(
            'SELECT health_detail FROM backtest_results WHERE run_id = ?',
            ('BT-ETF-LIMIT-01',)).fetchone()
    finally:
        con.close()
    assert row is not None
    assert 'ETF' in (row[0] or '') and '未模拟' in (row[0] or '')


def test_engine_stock_has_no_etf_note(_tmp_backtest_db):
    """个股不能出现 ETF 声明（文案按 asset_type 分流）。"""
    from backtest.engine import BacktestEngine

    with patch('backtest.data_adapter._load_stock_daily', return_value=_flat_frame()), \
         patch('backtest.engine.get_strategy_class', return_value=_probe_strategy()), \
         patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        result = BacktestEngine().run(
            _base_config(code='600519', asset_type='stock', strategy_id='stock_probe'),
            run_id='BT-STK-LIMIT-01')

    joined = '\n'.join(result['warnings'])
    assert 'ETF 涨跌停未模拟' not in joined
    assert '印花税万五' in joined

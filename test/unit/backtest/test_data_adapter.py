"""Test: data_adapter 核心逻辑

覆盖：列名规范化、复权计算、数据加载逻辑。
使用 mock 替代数据库操作。
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock


# ── _normalize_columns ──

class TestNormalizeColumns:

    def test_sets_trade_date_as_index(self):
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03'],
            'open': [10, 11], 'high': [11, 12], 'low': [9, 10],
            'close': [10.5, 11.5], 'volume': [1000, 2000],
        })
        result = _normalize_columns(df)
        assert result.index.name == 'trade_date'
        assert 'trade_date' not in result.columns

    def test_fills_volume_na(self):
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({
            'trade_date': ['2025-01-02'],
            'open': [10], 'high': [11], 'low': [9],
            'close': [10.5], 'volume': [np.nan],
        })
        result = _normalize_columns(df)
        assert result['volume'].iloc[0] == 0

    def test_raises_on_missing_columns(self):
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({'trade_date': ['2025-01-02'], 'open': [10]})
        with pytest.raises(ValueError, match='缺少必要列'):
            _normalize_columns(df)


# ── _apply_adjustment ──

class TestApplyAdjustment:

    def _make_df(self):
        return pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'open': [10.0, 11.0, 12.0],
            'high': [10.5, 11.5, 12.5],
            'low': [9.5, 10.5, 11.5],
            'close': [10.2, 11.2, 12.2],
        })

    def _make_adj_df(self):
        return pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'fore_adjust_factor': [1.0, 1.0, 1.0],
            'back_adjust_factor': [0.5, 0.5, 0.5],
        })

    def test_none_adjust_returns_original(self):
        from backtest.data_adapter import _apply_adjustment
        df = self._make_df()
        adj_df = self._make_adj_df()

        with patch('backtest.data_adapter.get_market_data_db') as mock_db:
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch('pandas.read_sql_query', return_value=adj_df):
                result = _apply_adjustment(df.copy(), '600519', 'none')

        # none 模式应返回原始数据，不含复权因子列
        assert 'fore_adjust_factor' not in result.columns
        assert 'back_adjust_factor' not in result.columns

    def test_qfq_normalizes_to_latest(self):
        """前复权：最新价格不变"""
        from backtest.data_adapter import _apply_adjustment
        df = self._make_df()
        adj_df = pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'fore_adjust_factor': [0.8, 0.9, 1.0],
            'back_adjust_factor': [0.4, 0.45, 0.5],
        })

        with patch('backtest.data_adapter.get_market_data_db') as mock_db:
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch('pandas.read_sql_query', return_value=adj_df):
                result = _apply_adjustment(df.copy(), '600519', 'qfq')

        # 前复权后，最新价格应不变（因子归一化到 1.0）
        assert abs(result['close'].iloc[-1] - 12.2) < 0.01
        # 最早价格应被调整（因子 0.8/1.0 = 0.8）
        assert abs(result['close'].iloc[0] - 10.2 * 0.8) < 0.01

    def test_hfq_multiplies_by_factor(self):
        """后复权：价格乘以后复权因子"""
        from backtest.data_adapter import _apply_adjustment
        df = self._make_df()
        adj_df = pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'fore_adjust_factor': [1.0, 1.0, 1.0],
            'back_adjust_factor': [0.5, 0.5, 0.5],
        })

        with patch('backtest.data_adapter.get_market_data_db') as mock_db:
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch('pandas.read_sql_query', return_value=adj_df):
                result = _apply_adjustment(df.copy(), '600519', 'hfq')

        # 后复权：价格 * 0.5
        assert abs(result['close'].iloc[0] - 10.2 * 0.5) < 0.01

    def test_no_adjust_factors_returns_original(self):
        """无复权因子时返回原始数据"""
        from backtest.data_adapter import _apply_adjustment
        df = self._make_df()

        with patch('backtest.data_adapter.get_market_data_db') as mock_db:
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch('pandas.read_sql_query', return_value=pd.DataFrame()):
                result = _apply_adjustment(df.copy(), '600519', 'qfq')

        # 无因子时价格不变
        assert abs(result['close'].iloc[0] - 10.2) < 0.01


# ── _is_data_insufficient ──

class TestIsDataInsufficient:

    def test_empty_df_is_insufficient(self):
        from backtest.data_adapter import _is_data_insufficient
        assert _is_data_insufficient(pd.DataFrame(), '2025-01-01', '2025-01-31') is True

    def test_short_data_is_insufficient(self):
        from backtest.data_adapter import _is_data_insufficient
        df = pd.DataFrame({'close': range(5)})
        assert _is_data_insufficient(df, '2025-01-01', '2025-01-31') is True

    def test_sufficient_data(self):
        from backtest.data_adapter import _is_data_insufficient
        df = pd.DataFrame({'close': range(50)})
        assert _is_data_insufficient(df, '2025-01-01', '2025-01-31') is False


# ── _estimate_expected_days ──

class TestEstimateExpectedDays:

    def test_estimates_trading_days(self):
        from backtest.data_adapter import _estimate_expected_days
        days = _estimate_expected_days('2025-01-01', '2025-01-31')
        assert 18 <= days <= 25

    def test_same_day_returns_one(self):
        from backtest.data_adapter import _estimate_expected_days
        days = _estimate_expected_days('2025-01-02', '2025-01-02')
        assert days == 1

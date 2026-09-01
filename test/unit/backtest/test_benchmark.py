"""基准对比和数据自愈测试"""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock


class TestBenchmarkReturn:
    """基准收益计算测试"""

    def test_calc_benchmark_return_normal(self):
        """测试正常基准收益计算"""
        from backtest.engine import _calc_benchmark_return

        mock_df = pd.DataFrame({
            'trade_date': ['2025-01-01', '2025-01-02', '2025-01-03'],
            'close': [100.0, 105.0, 110.0],
        })

        with patch('utils.cache.market_data_db.get_index_daily', return_value=mock_df):
            result = _calc_benchmark_return('000300', '2025-01-01', '2025-01-03')
            assert result is not None
            assert abs(result - 0.1) < 0.001  # 10% 收益

    def test_calc_benchmark_return_empty(self):
        """测试空数据"""
        from backtest.engine import _calc_benchmark_return

        with patch('utils.cache.market_data_db.get_index_daily', return_value=pd.DataFrame()):
            result = _calc_benchmark_return('000300', '2025-01-01', '2025-01-03')
            assert result is None

    def test_calc_benchmark_return_single_row(self):
        """测试单行数据"""
        from backtest.engine import _calc_benchmark_return

        mock_df = pd.DataFrame({
            'trade_date': ['2025-01-01'],
            'close': [100.0],
        })

        with patch('utils.cache.market_data_db.get_index_daily', return_value=mock_df):
            result = _calc_benchmark_return('000300', '2025-01-01', '2025-01-03')
            assert result is None  # 不足2行


class TestTradingCalendar:
    """交易日历集成测试"""

    def test_adjust_dates_weekend(self):
        """测试周末日期校正"""
        from backtest.engine import _adjust_dates_by_calendar
        from datetime import date

        config = {
            'start_date': '2025-06-07',  # 周六
            'end_date': '2025-06-08',    # 周日
        }

        with patch('utils.trading_calendar.is_market_open', return_value=False), \
             patch('utils.trading_calendar.get_next_trading_date', return_value=date(2025, 6, 9)):
            warnings = _adjust_dates_by_calendar(config)
            # 日期应该被校正
            assert config['start_date'] != '2025-06-07' or config['end_date'] != '2025-06-08'

    def test_adjust_dates_normal(self):
        """测试正常日期不校正"""
        from backtest.engine import _adjust_dates_by_calendar

        config = {
            'start_date': '2025-06-02',  # 周一
            'end_date': '2025-06-06',    # 周五
        }

        with patch('utils.trading_calendar.is_market_open', return_value=True):
            warnings = _adjust_dates_by_calendar(config)
            assert config['start_date'] == '2025-06-02'
            assert config['end_date'] == '2025-06-06'


class TestDataSelfHeal:
    """数据自愈测试"""

    def test_is_data_insufficient_empty(self):
        """测试空数据判断"""
        from backtest.data_adapter import _is_data_insufficient

        df = pd.DataFrame()
        assert _is_data_insufficient(df, '2025-01-01', '2025-12-31') is True

    def test_is_data_insufficient_normal(self):
        """测试正常数据量判断"""
        from backtest.data_adapter import _is_data_insufficient

        # 250天数据，预期约178个交易日（250*5/7），50%是89
        df = pd.DataFrame({'close': range(200)})
        assert _is_data_insufficient(df, '2025-01-01', '2025-12-31') is False

    def test_is_data_insufficient_low(self):
        """测试数据量不足判断"""
        from backtest.data_adapter import _is_data_insufficient

        # 只有10条数据，预期约178，50%是89
        df = pd.DataFrame({'close': range(10)})
        assert _is_data_insufficient(df, '2025-01-01', '2025-12-31') is True

    def test_estimate_expected_days(self):
        """测试预期交易日估算"""
        from backtest.data_adapter import _estimate_expected_days

        # 365天 * 5/7 ≈ 260
        days = _estimate_expected_days('2025-01-01', '2026-01-01')
        assert 250 <= days <= 270

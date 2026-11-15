"""`_min_kline_rows` 区间比例下限 + 日线频率闸门（2026-10 月线事故回归）。

事故：``stock_daily`` 的 ``000300`` 被写入 24 根**月末** bar（source=mx_data），
旧 `_min_kline_rows` 固定上限 20 让它们通过校验；回测再把月线当日线撮合。
"""
import numpy as np
import pandas as pd
import pytest

from utils.cache import market_data_db as mdb


def _frame(dates) -> pd.DataFrame:
    dates = pd.to_datetime(list(dates))
    n = len(dates)
    return pd.DataFrame({
        'open': np.linspace(1.0, 2.0, n),
        'high': np.linspace(1.1, 2.1, n),
        'low': np.linspace(0.9, 1.9, n),
        'close': np.linspace(1.0, 2.0, n),
        'volume': [100.0] * n,
    }, index=dates)


class TestMinKlineRowsByDays:
    """一年窗口的下限必须远高于 20（否则 24 根月线能通过）。"""

    def test_one_year_floor_is_at_least_73(self):
        assert mdb._min_kline_rows(365) >= 73
        assert mdb._min_kline_rows(365) > 24

    @pytest.mark.parametrize("days,expected", [
        (1, 1), (5, 1), (20, 20), (40, 20), (60, 20), (365, 73), (500, 100),
    ])
    def test_thresholds(self, days, expected):
        assert mdb._min_kline_rows(days) == expected


class TestMedianGap:
    def test_monthly_gap_detected(self):
        gap = mdb._median_kline_gap_days(
            _frame(pd.date_range('2024-11-29', periods=12, freq='ME')))
        assert gap is not None and 28 <= gap <= 31

    def test_daily_gap_is_one(self):
        gap = mdb._median_kline_gap_days(
            _frame(pd.bdate_range('2024-01-02', periods=60)))
        assert gap == 1.0

    def test_too_few_rows_returns_none(self):
        assert mdb._median_kline_gap_days(_frame(['2024-01-02', '2024-01-03'])) is None


class TestEnsureEnoughHistoryRows:
    def test_monthly_bars_rejected_as_non_daily(self):
        df = _frame(pd.date_range('2024-11-29', periods=24, freq='ME'))
        with pytest.raises(ValueError) as ei:
            mdb._ensure_enough_history_rows(df, 365, '000300', 'mx_data')
        assert '不是日线' in str(ei.value)

    def test_daily_bars_pass(self):
        df = _frame(pd.bdate_range('2024-01-02', periods=100))
        mdb._ensure_enough_history_rows(df, 365, '600519', 'akshare')

    def test_short_daily_still_rejected_by_row_count(self):
        df = _frame(pd.bdate_range('2024-01-02', periods=30))
        with pytest.raises(ValueError, match='K 线过少'):
            mdb._ensure_enough_history_rows(df, 365, '600519', 'akshare')

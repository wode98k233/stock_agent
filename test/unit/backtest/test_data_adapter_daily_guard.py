"""频率闸门回归：月线/周线快照不得进入日线回测。

背景（2026-10 事故）：``stock_daily`` 的 ``000300`` 只有 24 根**月末** bar
（source=mx_data），``_load_stock_daily`` 仅以 ``df.empty`` 判定兜底 →
24 行非空即永久短路，回测把「月末买、次月卖」当「尾盘买、次日卖」。
"""
import numpy as np
import pandas as pd
import pytest

from backtest import data_adapter


def _bars(dates) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame({
        'code': ['000300'] * n,
        'trade_date': pd.to_datetime(dates),
        'open': np.linspace(3000, 3200, n),
        'high': np.linspace(3010, 3210, n),
        'low': np.linspace(2990, 3190, n),
        'close': np.linspace(3005, 3205, n),
        'pre_close': np.nan,
        'pct_change': [0.1] * n,
        'volume': [1e8] * n,
        'amount': [1e10] * n,
    })


# 月末 bar：间隔 28~31 天，共 24 根（与库内 000300 被污染的数据同构）
_MONTH_END = pd.date_range('2024-11-29', periods=24, freq='ME')
# 正常日线：工作日，含周末自然间隔（最大 3 天）
_DAILY = pd.bdate_range('2024-01-02', periods=60)


class TestDailyFrequencyGuard:

    def test_monthly_bars_rejected(self, monkeypatch):
        monkeypatch.setattr(data_adapter, '_load_stock_daily',
                            lambda *a, **k: _bars(_MONTH_END))
        with pytest.raises(ValueError) as ei:
            data_adapter.BacktraderDataFeed.from_stock_daily(
                '000300', '2023-10-19', '2026-10-18')
        msg = str(ei.value)
        assert '不是日线' in msg
        assert '000300' in msg
        assert '请勿用月线/周线快照做日线回测' in msg

    def test_weekly_bars_rejected(self, monkeypatch):
        dates = pd.date_range('2024-01-05', periods=30, freq='W-FRI')
        monkeypatch.setattr(data_adapter, '_load_stock_daily',
                            lambda *a, **k: _bars(dates))
        with pytest.raises(ValueError, match='不是日线'):
            data_adapter.BacktraderDataFeed.from_stock_daily(
                '000300', '2024-01-01', '2024-12-31')

    def test_daily_bars_pass(self, monkeypatch):
        monkeypatch.setattr(data_adapter, '_load_stock_daily',
                            lambda *a, **k: _bars(_DAILY))
        feed = data_adapter.BacktraderDataFeed.from_stock_daily(
            '600519', '2024-01-01', '2024-04-01')
        assert len(feed.p.dataname) == len(_DAILY)

    def test_holiday_gaps_use_median(self, monkeypatch):
        """春节长假（~10 天）不误杀：中位数仍是 1 天。"""
        daily = pd.bdate_range('2024-01-02', periods=20)
        holiday = pd.bdate_range('2024-02-19', periods=40)
        dates = daily.append(holiday)   # 中间断开约 3 周（春节休市）
        monkeypatch.setattr(data_adapter, '_load_stock_daily',
                            lambda *a, **k: _bars(dates))
        feed = data_adapter.BacktraderDataFeed.from_stock_daily(
            '600519', '2024-01-01', '2024-04-30')
        assert len(feed.p.dataname) == len(dates)

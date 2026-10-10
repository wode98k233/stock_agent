"""`period`（周/月线）支持回归测试。

背景（2026-09-22 run 2bc160e7）：计划要求「日K + 周K + 月K」，但
`get_etf_history` 只有日线（`fund_etf_hist_em(period="daily")` 硬编码），周/月K 根本取不到；
且 250 条日K 一次返回 32,735 字符 → 被主动截断丢掉中段，
报告误写成「周K/月K 数据缺失（数据源截断 24,735 字符）」。
"""
import sys

import pandas as pd
import pytest

pytestmark = pytest.mark.unit


def _cn_df(n=10):
    return pd.DataFrame({
        "日期": ["2026-08-%02d" % (i + 1) for i in range(n)],
        "开盘": [1.0 + i * 0.01 for i in range(n)],
        "最高": [1.1 + i * 0.01 for i in range(n)],
        "最低": [0.9 + i * 0.01 for i in range(n)],
        "收盘": [1.05 + i * 0.01 for i in range(n)],
        "成交量": [1000.0 + i for i in range(n)],
        "成交额": [2000.0 + i for i in range(n)],
    })


class _BoomHithink:
    """同花顺替身：任何调用都视为「不该被调用」（周/月线 REST 不支持）。"""

    def get_stock_hist(self, *a, **k):
        raise AssertionError("该场景不应调用同花顺")


class _OkHithink:
    def __init__(self):
        self.periods = []

    def get_stock_hist(self, sym, start="", end="", **kw):
        self.periods.append(kw.get("period", "daily"))
        return _cn_df()


class _FakeAk:
    def __init__(self, df=None, sina_raises=False):
        self.calls = []
        self._df = df if df is not None else _cn_df()
        self._sina_raises = sina_raises

    def fund_etf_hist_em(self, symbol, period, start_date, end_date, adjust):
        self.calls.append(period)
        return self._df

    def fund_etf_hist_sina(self, symbol):
        if self._sina_raises:
            raise AssertionError("周/月线不得回退到新浪日线端点")
        self.calls.append("sina")
        return self._df


class TestEtfHistoryPeriod:
    def test_daily_uses_hithink_main_channel(self, monkeypatch):
        import tools.fund_data as fd

        h = _OkHithink()
        monkeypatch.setattr(fd, "_hithink", lambda: h)

        rows = fd.get_etf_history("159570", days=5)
        assert len(rows) == 5
        assert h.periods == ["daily"]

    def test_weekly_skips_hithink_and_passes_period_to_dongcai(self, monkeypatch):
        import tools.fund_data as fd

        fake = _FakeAk()
        monkeypatch.setattr(fd, "_hithink", lambda: _BoomHithink())
        monkeypatch.setitem(sys.modules, "akshare", fake)

        rows = fd.get_etf_history("159570", days=4, period="weekly")
        assert fake.calls[0] == "weekly"
        assert len(rows) == 4

    def test_monthly_passes_period(self, monkeypatch):
        import tools.fund_data as fd

        fake = _FakeAk()
        monkeypatch.setattr(fd, "_hithink", lambda: _BoomHithink())
        monkeypatch.setitem(sys.modules, "akshare", fake)

        fd.get_etf_history("510300", days=3, period="monthly")
        assert fake.calls[0] == "monthly"

    def test_invalid_period_falls_back_to_daily(self, monkeypatch):
        import tools.fund_data as fd

        fake = _FakeAk()
        monkeypatch.setattr(fd, "_hithink", lambda: _OkHithink())
        monkeypatch.setitem(sys.modules, "akshare", fake)

        fd.get_etf_history("510300", days=3, period="yearly")
        assert "yearly" not in fake.calls

    def test_weekly_returns_empty_when_no_source(self, monkeypatch):
        """周/月线取不到时必须返回空，**不得拿日线冒充周线**。"""
        import tools.fund_data as fd

        monkeypatch.setattr(fd, "_hithink", lambda: _BoomHithink())
        monkeypatch.setitem(sys.modules, "akshare", _FakeAk(df=pd.DataFrame(), sina_raises=True))

        assert fd.get_etf_history("159570", days=30, period="weekly") == []

    def test_period_window_scales_for_weekly(self, monkeypatch):
        """days 是条数：周线要放大自然日窗口，否则取不满。"""
        import tools.fund_data as fd

        captured = {}

        class _Ak:
            def fund_etf_hist_em(self, symbol, period, start_date, end_date, adjust):
                captured["start"] = start_date
                captured["period"] = period
                return _cn_df(4)

        monkeypatch.setattr(fd, "_hithink", lambda: _BoomHithink())
        monkeypatch.setitem(sys.modules, "akshare", _Ak())

        fd.get_etf_history("159570", days=30, period="weekly")
        assert captured["period"] == "weekly"
        # 30 周 ≈ 210 天，窗口起点必须明显早于「30 天」
        from datetime import datetime
        span_days = (datetime.strptime(datetime.now().strftime("%Y%m%d"), "%Y%m%d")
                     - datetime.strptime(captured["start"], "%Y%m%d")).days
        assert span_days > 200


class TestStockHistoryPeriod:
    def test_non_daily_bypasses_cache_and_passes_period(self, monkeypatch):
        """非日线：绕过本地缓存（缓存键不含 period），并把 period 透传给 fetcher。"""
        import tools.stock_data as sd

        seen = {}

        def _fake_ak_stock_hist(symbol, period="daily", start="", end=""):
            seen["period"] = period
            return _cn_df(8)

        def _no_cache(*a, **k):
            raise AssertionError("非日线不应读缓存")

        monkeypatch.setattr(sd, "ak_stock_hist", _fake_ak_stock_hist)
        monkeypatch.setattr(sd, "get_history_cache", _no_cache)

        df = sd.get_stock_history("600519", days=8, period="weekly")
        assert seen["period"] == "weekly"
        assert len(df) == 8

    def test_non_daily_returns_empty_on_failure(self, monkeypatch):
        import tools.stock_data as sd

        monkeypatch.setattr(sd, "ak_stock_hist",
                            lambda symbol, period="daily", start="", end="": pd.DataFrame())
        monkeypatch.setattr(sd, "get_history_cache", lambda *a, **k: None)

        df = sd.get_stock_history("600519", days=8, period="monthly")
        assert df is None or df.empty

    def test_invalid_period_falls_back_to_daily(self, monkeypatch):
        import tools.stock_data as sd

        seen = {}

        def _fake(symbol, period="daily", start="", end=""):
            seen["period"] = period
            return _cn_df(5)

        monkeypatch.setattr(sd, "ak_stock_hist", _fake)
        monkeypatch.setattr(sd, "get_history_cache", lambda *a, **k: None)
        monkeypatch.setattr(sd, "set_history_cache", lambda *a, **k: None)

        sd.get_stock_history("600519", days=5, period="bogus")
        assert seen["period"] == "daily"

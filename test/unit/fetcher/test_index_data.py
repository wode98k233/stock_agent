"""指数估值 / 中证指数日线数据源测试 — mock akshare，纯本地。

覆盖：
- 中证指数代码判定（H + 纯数字）不得与港股指数（H + 字母）混淆
- get_index_valuation：列映射 + 源日期倒序 → 输出升序 + 缺列/空表报错
- get_index_hist_csindex：统一列名 + 起止日透传 + 缺收盘报错
- 基类桩（未实现的源跳过不熔断）
"""
from unittest.mock import MagicMock

import pandas as pd
import pytest


@pytest.fixture
def fake_ak(monkeypatch):
    """mock AkshareDataSource._ak，避免真实 akshare 网络调用。"""
    from tools.fetcher.akshare_ds import AkshareDataSource
    ak = MagicMock()
    monkeypatch.setattr(AkshareDataSource, "_ak", ak)
    monkeypatch.setattr(AkshareDataSource, "_enforce_rate_limit", classmethod(
        lambda cls, **kw: None))
    return ak


# ---------------------------------------------------------------------------
# 代码判定
# ---------------------------------------------------------------------------

class TestCsindexCodeDetection:
    def test_h_plus_digits_is_csindex(self):
        from tools.fetcher.akshare_ds import _is_csindex_code
        assert _is_csindex_code("H30269") is True
        assert _is_csindex_code("h30269") is True
        assert _is_csindex_code(" H30374 ") is True

    def test_hk_and_exchange_codes_are_not_csindex(self):
        from tools.fetcher.akshare_ds import _is_csindex_code
        for code in ("HSTECH", "HSI", "hkHSTECH", "000300", "sh000015", "399001", ""):
            assert _is_csindex_code(code) is False, code


# ---------------------------------------------------------------------------
# get_index_valuation
# ---------------------------------------------------------------------------

class TestGetIndexValuation:
    @staticmethod
    def _source_df():
        """真实源是日期倒序（最新在前）。"""
        return pd.DataFrame([
            {"日期": "2026-09-16", "指数代码": "H30269", "股息率1": 4.31,
             "市盈率1": 7.90, "股息率2": 4.80, "市盈率2": 7.70},
            {"日期": "2026-09-15", "指数代码": "H30269", "股息率1": 4.35,
             "市盈率1": 7.92, "股息率2": 4.82, "市盈率2": 7.72},
            {"日期": "2026-08-20", "指数代码": "H30269", "股息率1": 4.38,
             "市盈率1": 7.93, "股息率2": 4.84, "市盈率2": 7.77},
        ])

    def test_columns_and_ascending_order(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_value_csindex.return_value = self._source_df()
        df = AkshareDataSource.get_index_valuation("H30269")
        assert {"report_date", "dy", "pe"} <= set(df.columns)
        # 源倒序 → 输出升序（与 get_macro_indicator 口径一致）
        assert list(df["report_date"]) == ["2026-08-20", "2026-09-15", "2026-09-16"]
        assert df.iloc[-1]["dy"] == pytest.approx(4.31)
        assert df.iloc[-1]["pe"] == pytest.approx(7.90)

    def test_alt_columns_preserved_without_guessing(self, fake_ak):
        """源未明示 1/2 口径差异 → 双列原样保留（dy_alt/pe_alt）。"""
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_value_csindex.return_value = self._source_df()
        df = AkshareDataSource.get_index_valuation("H30269")
        assert df.iloc[-1]["dy_alt"] == pytest.approx(4.80)
        assert df.iloc[-1]["pe_alt"] == pytest.approx(7.70)

    def test_duplicate_dates_deduped(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        src = self._source_df()
        fake_ak.stock_zh_index_value_csindex.return_value = pd.concat(
            [src, src.iloc[[0]]], ignore_index=True)
        df = AkshareDataSource.get_index_valuation("H30269")
        assert len(df) == 3
        assert df["report_date"].is_unique

    def test_empty_raises(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_value_csindex.return_value = pd.DataFrame()
        with pytest.raises(RuntimeError):
            AkshareDataSource.get_index_valuation("H30269")

    def test_missing_dividend_column_raises_with_hint(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_value_csindex.return_value = pd.DataFrame(
            {"日期": ["2026-09-16"], "别的列": [1.0]})
        with pytest.raises(RuntimeError, match="H30269"):
            AkshareDataSource.get_index_valuation("H30269")

    def test_base_class_stub(self):
        from tools.fetcher.base import DataSource
        with pytest.raises(NotImplementedError):
            DataSource.get_index_valuation("H30269")


# ---------------------------------------------------------------------------
# get_index_hist_csindex
# ---------------------------------------------------------------------------

class TestGetIndexHistCsindex:
    @staticmethod
    def _source_df():
        return pd.DataFrame({
            "日期": ["2026-09-16", "2026-09-17"],
            "指数代码": ["H30269", "H30269"],
            "开盘": [11001.97, 10909.94],
            "最高": [11032.07, 10966.20],
            "最低": [10871.26, 10846.72],
            "收盘": [10931.94, 10890.55],
            "涨跌": [-89.38, -41.39],
            "涨跌幅": [-0.81, -0.38],
            "成交量": [2.999209e9, 2.863194e9],
            "成交金额": [318.73, 294.85],
            "滚动市盈率": [7.92, 7.90],
        })

    def test_columns_normalized(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_hist_csindex.return_value = self._source_df()
        df = AkshareDataSource.get_index_hist_csindex("H30269", "20250101", "20260917")
        last = df.iloc[-1]
        assert last["report_date"] == "2026-09-17"
        assert last["close"] == pytest.approx(10890.55)
        assert last["open"] == pytest.approx(10909.94)
        assert last["amount"] == pytest.approx(294.85)
        assert last["pct_change"] == pytest.approx(-0.38)
        assert last["change_amount"] == pytest.approx(-41.39)
        assert last["pe_ttm"] == pytest.approx(7.90)

    def test_date_range_passed_through_normalized(self, fake_ak):
        """起止日须去掉连字符（接口要 YYYYMMDD）。"""
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_hist_csindex.return_value = self._source_df()
        AkshareDataSource.get_index_hist_csindex("H30269", "2025-01-01", "2026-09-17")
        kwargs = fake_ak.stock_zh_index_hist_csindex.call_args.kwargs
        assert kwargs["symbol"] == "H30269"
        assert kwargs["start_date"] == "20250101"
        assert kwargs["end_date"] == "20260917"

    def test_missing_close_raises_with_hint(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_hist_csindex.return_value = pd.DataFrame(
            {"日期": ["2026-09-17"], "别的列": [1.0]})
        with pytest.raises(RuntimeError, match="H30269"):
            AkshareDataSource.get_index_hist_csindex("H30269")

    def test_empty_raises(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.stock_zh_index_hist_csindex.return_value = pd.DataFrame()
        with pytest.raises(RuntimeError):
            AkshareDataSource.get_index_hist_csindex("H30269")

    def test_base_class_stub(self):
        from tools.fetcher.base import DataSource
        with pytest.raises(NotImplementedError):
            DataSource.get_index_hist_csindex("H30269")


# ---------------------------------------------------------------------------
# 对外出口
# ---------------------------------------------------------------------------

class TestFetcherExports:
    def test_new_exports_declared_and_callable(self):
        """新增能力必须进 __all__，否则 `from tools.fetcher import *` 拿不到。"""
        import tools.fetcher as pkg
        for name in ("ak_index_valuation", "ak_index_hist_csindex"):
            assert name in pkg.__all__, name
            assert callable(getattr(pkg, name))


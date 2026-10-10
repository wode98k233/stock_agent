"""历史K线「短序列 / 快照污染」防护单测（2026-09-20 实测事故）。

事故链（四层，本文件覆盖后三层）：
1. `mx_data` 数据源把「当日一行实时快照」当作历史K线返回（非空 → 不触发换源）；
2. `@retry` 的列校验只查 `日期/收盘` 存在，不查行数 → 坏数据一路带下去；
3. 该 1 行结果被写入历史K线缓存，校验同样只查列 → 毒缓存持续命中；
4. `tools/tech_indicators.calc_indicators` 在短序列上崩（先 `obv[-6]` IndexError，
   后 `int(vol_ma5[-1])` 的 ValueError: cannot convert float NaN to integer）。

对应修复：`tools/fetcher._MIN_HISTORY_ROWS`（判失败以换源）、
`tools/stock_data.MIN_HISTORY_CACHE_ROWS`（缓存拒收）、
`tools/tech_indicators.MIN_INDICATOR_ROWS`（不足则抛可读错误而非崩）。
"""
import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.unit


def _history_df(rows: int) -> pd.DataFrame:
    """标准化后的历史K线（英文列，供 calc_indicators / 缓存校验使用）。"""
    idx = pd.date_range("2026-01-01", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "open": np.linspace(100, 110, rows),
            "close": np.linspace(101, 111, rows),
            "high": np.linspace(102, 112, rows),
            "low": np.linspace(99, 109, rows),
            "volume": np.linspace(1e6, 2e6, rows),
        },
        index=idx,
    )


def _raw_hist_df(rows: int) -> pd.DataFrame:
    """数据源原始输出（中文列，供 fetcher 层校验使用）。"""
    df = _history_df(rows).reset_index().rename(columns={"index": "日期"})
    return df.rename(columns={"open": "开盘", "close": "收盘", "high": "最高",
                              "low": "最低", "volume": "成交量"})


class TestHistoryCacheRowGuard:
    """缓存校验必须拒绝「字段齐全但行数过少」的伪历史。"""

    def test_single_row_cache_rejected(self):
        from tools.stock_data import _validate_normalized_history_cache

        valid, reason = _validate_normalized_history_cache(_history_df(1))
        assert valid is False
        assert "行数不足" in reason

    def test_zero_row_cache_rejected(self):
        from tools.stock_data import _validate_normalized_history_cache

        valid, _ = _validate_normalized_history_cache(_history_df(0))
        assert valid is False

    def test_normal_cache_accepted(self):
        from tools.stock_data import _validate_normalized_history_cache

        valid, reason = _validate_normalized_history_cache(_history_df(60))
        assert valid is True, reason


class TestFetcherRowGuard:
    """数据源返回短序列时必须判失败，以便 @retry 换源。"""

    def _patch_source(self, monkeypatch, source_cls):
        import tools.fetcher as fetcher
        from tools.fetcher import DataSourceManager

        monkeypatch.setattr(DataSourceManager, "get_available_sources",
                            classmethod(lambda cls, capability=None: [source_cls]))
        monkeypatch.setattr(fetcher, "_ensure_initialized", lambda: None)

    def test_ak_stock_hist_raises_on_single_row(self, monkeypatch):
        import tools.fetcher as fetcher
        from tools.fetcher.base import DataSource

        class _ShortSource(DataSource):
            name = "short_src"
            priority = 999

            @classmethod
            def get_stock_hist(cls, symbol, period="daily", start="", end=""):
                return _raw_hist_df(1)

        self._patch_source(monkeypatch, _ShortSource)

        with pytest.raises(Exception) as ei:
            fetcher.ak_stock_hist("600519", "daily", "20260101", "20260920")
        assert "行数不足" in str(ei.value)

    def test_ak_stock_hist_passes_normal_result(self, monkeypatch):
        import tools.fetcher as fetcher
        from tools.fetcher.base import DataSource

        class _GoodSource(DataSource):
            name = "good_src"
            priority = 999

            @classmethod
            def get_stock_hist(cls, symbol, period="daily", start="", end=""):
                return _raw_hist_df(60)

        self._patch_source(monkeypatch, _GoodSource)

        df = fetcher.ak_stock_hist("600519", "daily", "20260101", "20260920")
        assert len(df) == 60


class TestIndicatorsShortSeries:
    """指标计算对短序列必须给出可读错误，不得崩在某个下标/NaN 转换上。"""

    def test_single_row_raises_readable_error(self):
        from tools.tech_indicators import calc_indicators

        with pytest.raises(ValueError) as ei:
            calc_indicators(_history_df(1))
        assert "交易日" in str(ei.value)

    def test_short_series_raises_readable_error(self):
        from tools.tech_indicators import calc_indicators

        with pytest.raises(ValueError):
            calc_indicators(_history_df(3))

    def test_normal_series_computes_all_indicators(self):
        from tools.tech_indicators import calc_indicators

        ind = calc_indicators(_history_df(120))
        assert ind.get("obv_trend") in ("上升", "下降")
        for key in ("rsi", "dif", "dea", "macd_bar", "ma5", "ma10", "ma20", "ma60"):
            assert key in ind
            # 正常序列不得出现 NaN（NaN 会在报告里渲染成 nan）
            assert not (isinstance(ind[key], float) and np.isnan(ind[key])), key

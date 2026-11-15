"""复权因子反推与并入（utils/cache/adjust_factors.py）的纯逻辑测试。

锁住三件事（都来自 2026-11-12 的真实数据审计）：

1. 因子 = qfq_close / raw_close，且只在同一天上算；
2. 本地序列属于 raw 还是 qfq 必须**比**出来（600519 是原始价、000001 已是前复权，
   口径混在同一张表里）；
3. 因子缺日期时要前后向填充——只乘命中的日期会让同一条 K 线一半复权、一半不复权。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from utils.cache import adjust_factors as af


def _series(dates, closes):
    return pd.DataFrame({"trade_date": list(dates), "close": list(closes)})


class TestSymbol:
    @pytest.mark.parametrize("code,symbol", [
        ("600519", "sh600519"),
        ("sh600519", "sh600519"),
        ("688981", "sh688981"),
        ("000001", "sz000001"),
        ("300308", "sz300308"),
        ("159570", "sz159570"),
        ("510300", "sh510300"),
        ("830799", "bj830799"),
    ])
    def test_market_prefix(self, code, symbol):
        assert af._symbol(code) == symbol


class TestDetectBasis:
    def _pair(self):
        dates = [f"2026-01-{i:02d}" for i in range(1, 11)]
        raw = pd.DataFrame({"trade_date": dates, "close": np.full(10, 100.0)})
        # 前复权：除权前的价格整体低 2%（1/100）
        qfq = pd.DataFrame({"trade_date": dates,
                            "close": np.array([98.0] * 5 + [100.0] * 5)})
        return dates, raw, qfq

    def test_raw_series_is_detected(self):
        dates, raw, qfq = self._pair()
        stored = _series(dates, raw["close"])
        assert af.detect_basis(stored, raw, qfq) == "raw"

    def test_qfq_series_is_detected(self):
        dates, raw, qfq = self._pair()
        stored = _series(dates, qfq["close"])
        assert af.detect_basis(stored, raw, qfq) == "qfq"

    def test_identical_raw_and_qfq_defaults_to_raw(self):
        """没有分红时两条序列一样，按 raw 处理（乘 1.0 因子等价，不会误伤）。"""
        dates, raw, _ = self._pair()
        stored = _series(dates, raw["close"])
        assert af.detect_basis(stored, raw, raw.copy()) == "raw"

    def test_empty_stored_falls_back_to_raw(self):
        assert af.detect_basis(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()) == "raw"


class TestDeriveFactors:
    def test_factor_is_ratio_of_qfq_to_raw(self, monkeypatch):
        dates = ["2026-06-01", "2026-06-02", "2026-06-03"]
        raw = pd.DataFrame({"trade_date": dates, "open": 10.0, "high": 10.0,
                            "low": 10.0, "close": [100.0, 101.0, 102.0]})
        qfq = raw.assign(close=[98.0, 99.0, 102.0])

        def fake_fetch(symbol, fq):
            assert symbol == "sh600519"
            return raw.copy() if fq == "none" else qfq.copy()

        monkeypatch.setattr(af, "_fetch", fake_fetch)
        factors, raw_out = af.derive_factors("600519")
        assert list(factors["trade_date"]) == dates
        assert factors["fore_adjust_factor"].tolist() == pytest.approx([0.98, 0.980198, 1.0])
        assert len(raw_out) == 3

    def test_empty_source_returns_empty(self, monkeypatch):
        monkeypatch.setattr(af, "_fetch", lambda s, fq: pd.DataFrame())
        factors, _ = af.derive_factors("600519")
        assert factors.empty


class TestApplyFactorSeries:
    def _df(self):
        return pd.DataFrame({
            "trade_date": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
            "open": [100.0] * 4, "high": [100.0] * 4,
            "low": [100.0] * 4, "close": [100.0] * 4,
        })

    def test_latest_bar_is_unchanged(self):
        """前复权归一到最新一根：最后一天的价格必须和原始价一致。"""
        factor_df = pd.DataFrame({
            "trade_date": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
            "factor": [0.9, 0.9, 1.0, 1.0],
        })
        out = af.apply_factor_series(self._df(), factor_df)
        assert out["close"].tolist() == pytest.approx([90.0, 90.0, 100.0, 100.0])

    def test_missing_dates_are_filled_forward(self):
        """因子只给了 1/1 与 1/4：中间两天必须沿用前值，不能一半不调整。"""
        factor_df = pd.DataFrame({"trade_date": ["2026-01-01", "2026-01-04"],
                                  "factor": [0.9, 1.0]})
        out = af.apply_factor_series(self._df(), factor_df)
        assert out["close"].tolist() == pytest.approx([90.0, 90.0, 90.0, 100.0])

    def test_rows_before_factor_history_use_earliest(self):
        """早于因子覆盖期的行用最早因子 bfill：整段等比缩放，不留缝。"""
        factor_df = pd.DataFrame({"trade_date": ["2026-01-03", "2026-01-04"],
                                  "factor": [0.9, 1.0]})
        out = af.apply_factor_series(self._df(), factor_df)
        assert out["close"].tolist() == pytest.approx([90.0, 90.0, 90.0, 100.0])

    def test_all_nan_factor_returns_original(self):
        df = self._df()
        factor_df = pd.DataFrame({"trade_date": ["2026-01-01"], "factor": [np.nan]})
        out = af.apply_factor_series(df, factor_df)
        assert out is df

    def test_empty_factor_returns_original(self):
        df = self._df()
        assert af.apply_factor_series(df, pd.DataFrame()) is df

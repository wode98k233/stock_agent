"""图表精度回归：低价 ETF 的均线不能因为四舍五入被压成台阶。

背景（2026-11-04 用户反馈）：159928 收盘价在 0.65~0.75，K 线接口把 OHLC 截到 2 位、
round_list 又把均线截到 2 位，导致 MA5 出现 85 个相邻同值，画出来是一段段平线。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from server.chart_utils import ma, round_list
from server.routes.charts import _calc_indicators


def _low_price_df(n: int = 120) -> pd.DataFrame:
    """构造一条 0.6~0.8 之间的低波幅价格序列（模拟 ETF）。"""
    rng = np.random.default_rng(20261104)
    close = 0.70 + np.cumsum(rng.normal(0, 0.0035, n))
    close = np.clip(close, 0.60, 0.85)
    high = close + np.abs(rng.normal(0.002, 0.001, n))
    low = close - np.abs(rng.normal(0.002, 0.001, n))
    return pd.DataFrame({
        "open": close + rng.normal(0, 0.001, n),
        "high": high,
        "low": low,
        "close": close,
        "volume": np.full(n, 1_000_000.0),
        "amount": np.full(n, 1_000_000.0),
        "pct_change": np.zeros(n),
        "turnover_rate": np.zeros(n),
    })


def test_round_list_keeps_four_decimals():
    out = round_list([0.6969999, 1.23456, 1446.4444])
    assert out == [0.697, 1.2346, 1446.4444]


def test_round_list_maps_nan_to_none():
    out = round_list([np.nan, 0.5])
    assert out[0] is None and out[1] == 0.5


def test_ma_has_no_long_flat_runs_on_low_price_series():
    """均线在低价位上不应出现「大段相同值」（台阶）。

    偶发两个相邻值相同时在数学上无法排除（价格几乎不动时均线本来就会相等），
    所以判定标准是「最长连续相同段」：2 位小数时实测 85 个相邻同值（整段平线），
    4 位小数下应当只剩零星一两个。
    """
    df = _low_price_df()
    ind = _calc_indicators(df)
    ma5 = [v for v in ind["ma5"] if v is not None]
    assert len(ma5) > 100

    max_run = run = 1
    for a, b in zip(ma5, ma5[1:]):
        run = run + 1 if a == b else 1
        max_run = max(max_run, run)

    assert max_run <= 3, f"MA5 最长连续相同段 {max_run} 个，均线仍会被画成台阶"


def test_ma_values_are_not_quantised_to_cent():
    """均线值的有效分辨率必须优于 0.01（即不能所有值都是两位小数）。"""
    df = _low_price_df()
    ind = _calc_indicators(df)
    ma5 = np.array([v for v in ind["ma5"] if v is not None])
    cents = np.round(ma5, 2)
    assert not np.allclose(ma5, cents), "MA5 仍被量化到 0.01，精度没生效"


def test_ma_matches_reference_computation():
    """精度修改不能改变算法本身：与直接调用 ma() 的结果一致。"""
    df = _low_price_df()
    ind = _calc_indicators(df)
    ref = ma(df["close"].values, 5)
    got = ind["ma5"]
    for i, v in enumerate(ref):
        if np.isnan(v):
            assert got[i] is None
        else:
            assert got[i] == pytest.approx(v, abs=1e-4)

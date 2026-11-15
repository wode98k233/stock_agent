"""factor_lib 单测。

三类断言最关键（不是覆盖率凑数）：
1. **零前视**：截断未来数据后，历史因子值必须逐位不变——这是因子评估最致命的坑。
2. **口径对账**：序列版指标在末值上必须与 ``tech_indicators`` 的标量版一致
   （DMI 因原实现 off-by-one，用容差）。
3. **量纲无关**：价格整体缩放后，乖离率/归一化 MACD 必须不变——否则截面排名
   变成在排股价。
"""
import numpy as np
import pandas as pd
import pytest

from backtest.factor_lib import (
    FACTOR_IDS,
    FACTOR_SPECS,
    atr_series,
    cci_series,
    compute_factors,
    dmi_series,
    psy_series,
    validate_frame,
    vr_series,
    wr_series,
)
from tools import tech_indicators as ti


# ------------------------------------------------------------ fixtures

def _frame(n: int = 260, seed: int = 7, scale: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0004, 0.018, n)
    close = 20.0 * scale * np.cumprod(1 + ret)
    high = close * (1 + rng.uniform(0.001, 0.02, n))
    low = close * (1 - rng.uniform(0.001, 0.02, n))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    vol = rng.uniform(1e6, 5e6, n)
    dates = pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "trade_date": dates,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": vol, "amount": vol * close,
    })


# ------------------------------------------------------------ 基础契约

def test_all_factors_compute_without_inf():
    df = _frame()
    out = compute_factors(df)
    assert list(out.columns) == FACTOR_IDS
    assert len(out) == len(df)
    assert len(FACTOR_IDS) >= 30, "因子池应 ≥30（PRD：技术指标体系批量评估）"
    for fid in FACTOR_IDS:
        col = out[fid]
        assert not np.isinf(col.to_numpy()).any(), f"{fid} 出现 inf"
        assert col.notna().sum() > 100, f"{fid} 有效值过少"


def test_unknown_factor_raises():
    with pytest.raises(KeyError, match="未知因子"):
        compute_factors(_frame(60), ["no_such_factor"])


def test_missing_columns_raises():
    with pytest.raises(ValueError, match="缺少必要列"):
        validate_frame(pd.DataFrame({"close": [1.0, 2.0]}))


def test_single_factor_failure_isolated(monkeypatch):
    """单因子抛错时该列为 NaN，其余因子照常（PRD §8：不中断整批）。"""
    df = _frame(120)

    def _boom(_df):
        raise RuntimeError("模拟因子崩溃")

    spec = FACTOR_SPECS["ret_5"]
    monkeypatch.setitem(FACTOR_SPECS, "ret_5",
                        type(spec)(**{**spec.__dict__, "fn": _boom}))
    out = compute_factors(df, ["ret_5", "rsi_14"])
    assert out["ret_5"].isna().all()
    assert out["rsi_14"].notna().sum() > 50


# ------------------------------------------------------------ 零前视（核心）

def test_no_lookahead_truncation_invariance():
    """把未来数据截掉，历史因子值必须完全不变。"""
    df = _frame(200)
    cut = 130
    full = compute_factors(df)
    part = compute_factors(df.iloc[:cut].reset_index(drop=True))

    for fid in FACTOR_IDS:
        a = full[fid].iloc[:cut].to_numpy(dtype=float)
        b = part[fid].to_numpy(dtype=float)
        both_nan = np.isnan(a) & np.isnan(b)
        diff = np.abs(np.nan_to_num(a, nan=0.0) - np.nan_to_num(b, nan=0.0))
        # 允许 1e-12 的浮点噪声（ewm 等）
        assert np.all(diff[~both_nan] < 1e-9), f"{fid} 存在前视（截断后历史值变化）"


# ------------------------------------------------------------ 口径对账

def test_series_match_scalar_counterparts():
    """序列版末值 == tech_indicators 标量版（同一口径）。"""
    df = _frame(120)
    h, l, c, v = (df["high"].values, df["low"].values,
                  df["close"].values, df["volume"].values)

    # ATR：标量版是「最近 period 根 TR 的均值」
    assert atr_series(h, l, c, 14).iloc[-1] == pytest.approx(
        ti._atr(h, l, c, 14), rel=1e-9)

    # CCI
    assert cci_series(h, l, c, 20).iloc[-1] == pytest.approx(
        ti._cci(h, l, c, 20), rel=1e-9, abs=1e-9)

    # 威廉 %R
    assert wr_series(h, l, c, 14).iloc[-1] == pytest.approx(
        ti._wr(h, l, c, 14), rel=1e-9)

    # PSY
    assert psy_series(c, 12).iloc[-1] == pytest.approx(ti._psy(c, 12), rel=1e-9)

    # VR
    assert vr_series(c, v, 26).iloc[-1] == pytest.approx(
        ti._vr(c, v, 26), rel=1e-9)


def test_dmi_series_close_to_scalar():
    """DMI 原实现把长度 n-1 的 DM 与长度 n 的 ATR 配对（差一根），
    序列版按正确对齐实现 → 末值有小差异，用相对容差断言。"""
    df = _frame(120)
    plus, minus, adx = dmi_series(df["high"].values, df["low"].values,
                                  df["close"].values, 14)
    s_plus, s_minus, s_dx = ti._dmi(df["high"].values, df["low"].values,
                                    df["close"].values, 14)
    assert plus.iloc[-1] == pytest.approx(s_plus, rel=0.05, abs=1.0)
    assert minus.iloc[-1] == pytest.approx(s_minus, rel=0.05, abs=1.0)
    assert 0 <= adx.iloc[-1] <= 100


# ------------------------------------------------------------ 量纲无关

def test_scale_invariance_for_normalized_factors():
    """价格整体 ×10：乖离率与归一化 MACD 必须不变（截面可比的前提）。"""
    df1 = _frame(160, seed=11, scale=1.0)
    df2 = df1.copy()
    for col in ("open", "high", "low", "close"):
        df2[col] = df2[col] * 10.0
    f1 = compute_factors(df1)
    f2 = compute_factors(df2)

    for fid in ("ma_bias_20", "ema_bias_12", "ma_spread_5_20",
                "macd_dif", "macd_dea", "macd_hist", "boll_pctb",
                "atr_pct", "close_position_20", "price_range_20"):
        a = f1[fid].to_numpy(dtype=float)
        b = f2[fid].to_numpy(dtype=float)
        mask = ~np.isnan(a) & ~np.isnan(b)
        assert np.allclose(a[mask], b[mask], rtol=1e-9, atol=1e-12), \
            f"{fid} 对价格量纲敏感（会导致截面排名退化为排股价）"


# ------------------------------------------------------------ 预热与边界

def test_warmup_nan_counts():
    df = _frame(100)
    out = compute_factors(df, ["ret_60", "ma_bias_60", "rsi_6", "ret_5"])
    # ret_60 前 60 个应为 NaN
    assert out["ret_60"].iloc[:60].isna().all()
    assert out["ret_60"].iloc[60:].notna().all()
    # rsi_6 前 6 个 NaN
    assert out["rsi_6"].iloc[:6].isna().all()
    assert out["rsi_6"].iloc[6:].notna().all()


def test_short_frame_returns_all_nan():
    df = _frame(10)
    out = compute_factors(df, ["ma_bias_20", "ret_5"])
    assert out["ma_bias_20"].isna().all()
    assert out["ret_5"].isna().all()


def test_flat_price_no_inf():
    """横盘（零波动）不能产生 inf/nan 爆炸——分母保护。"""
    n = 80
    flat = pd.DataFrame({
        "trade_date": pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d"),
        "open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n,
        "close": [10.0] * n, "volume": [1e6] * n, "amount": [1e7] * n,
    })
    out = compute_factors(flat)
    for fid in FACTOR_IDS:
        arr = out[fid].to_numpy(dtype=float)
        assert not np.isinf(arr).any(), f"{fid} 横盘时出现 inf"

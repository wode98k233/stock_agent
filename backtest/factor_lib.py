"""因子计算库：把技术指标转成「逐日可截面排名」的因子序列。

定位
----
``tools/tech_indicators.py`` 服务于「个股当日快照」（``calc_indicators`` 只返回
最后一行）。因子评估需要的是**每个交易日一个值**——才能在某一日做横截面排名、
和未来收益算 IC。本模块把指标层补齐成逐日序列，并注册成因子表。

与 tech_indicators 的关系（2026-10-17 实测后确定，勿凭记忆改）
------------------------------------------------------------
- **直接复用**其返回逐日数组的纯函数：``_sma/_ema/_rsi/_macd_calc/_bbands/
  _kdj/_obv``（实测均返回 len=输入长度的 ndarray 或三元组）。
- **重写为序列版**：``_atr/_cci/_wr/_dmi/_psy/_vr`` —— 这 6 个在
  tech_indicators 里**返回标量**（只服务最后一行快照），拿来做截面排名会得到
  一条水平线。本模块的序列版口径对齐它们的定义；其中 ``_dmi`` 原实现有
  一个 off-by-one（``plus_dm`` 长 n-1 却与长 n 的 ``atr`` 取末元素配对），
  序列版按正确对齐实现，末值会有微小差异——单测用容差断言，不做逐位相等。

设计约束
--------
1. **零前视**：所有窗口只用当日及之前的数据；前向收益由 ``factor_eval`` 负责，
   本模块绝不出现 ``shift(-n)``。
2. **量纲可比**：跨股票的横截面排名要求量纲无关，故 DIF/DEA/OBV 等原始值一律
   除以价格或成交量做归一化（原始 DIF 与股价成正比，直接排名等于在排股价）。
3. **不造数**：预热期不足返回 NaN，不做 0 填充（0 是有意义的因子值）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from tools.tech_indicators import (
    _bbands,
    _ema,
    _kdj,
    _macd_calc,
    _obv,
    _rsi,
    _sma,
)

# 计算任一因子所需的最少行数（含预热）。低于此值整票跳过因子计算。
MIN_FACTOR_ROWS = 20


# ============================================================
# 内部工具
# ============================================================

def _s(values, index) -> pd.Series:
    """包成与输入同索引的 float Series。"""
    arr = np.asarray(values, dtype=float)
    return pd.Series(arr, index=index, dtype="float64")


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    """除法，分母 0/NaN → NaN（不产生 inf，inf 会污染排名）。"""
    a = pd.Series(a, dtype="float64")
    b = pd.Series(b, dtype="float64")
    return a.where(b.abs() > 1e-12, np.nan) / b.where(b.abs() > 1e-12, np.nan)


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> pd.Series:
    prev_close = pd.Series(close).shift(1)
    hl = pd.Series(high) - pd.Series(low)
    hc = (pd.Series(high) - prev_close).abs()
    lc = (pd.Series(low) - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


# ============================================================
# 序列版指标（tech_indicators 里是标量版，此处补齐逐日序列）
# ============================================================

def atr_series(high, low, close, period: int = 14) -> pd.Series:
    """ATR 逐日序列 = TR 的简单移动平均（对齐 tech_indicators._atr 口径）。"""
    tr = _true_range(high, low, close)
    return tr.rolling(period).mean()


def cci_series(high, low, close, period: int = 20) -> pd.Series:
    """CCI 逐日序列 = (TP − MA(TP)) / (0.015 × MAD(TP))。"""
    tp = (pd.Series(high) + pd.Series(low) + pd.Series(close)) / 3.0
    ma = tp.rolling(period).mean()
    md = tp.rolling(period).apply(
        lambda x: np.nanmean(np.abs(x - np.nanmean(x))), raw=True)
    return _safe_div(tp - ma, 0.015 * md)


def wr_series(high, low, close, period: int = 14) -> pd.Series:
    """威廉指标 %R 逐日序列（负值，越接近 0 越强）。"""
    hh = pd.Series(high).rolling(period).max()
    ll = pd.Series(low).rolling(period).min()
    rng = hh - ll
    return _safe_div(hh - pd.Series(close), rng) * -100.0


def dmi_series(high, low, close, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """DMI 逐日序列 → (+DI, −DI, ADX)。

    对齐方式与 ``tech_indicators._dmi`` 不同：原实现把长度 n−1 的 DM 序列与
    长度 n 的 ATR 取末元素配对（差一根），此处按同索引对齐实现。
    """
    h = pd.Series(high).astype(float)
    l = pd.Series(low).astype(float)
    up = h.diff()
    down = -l.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=h.index).astype(float)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=h.index).astype(float)
    atr = _true_range(high, low, close).rolling(period).mean()
    plus_di = _safe_div(plus_dm.rolling(period).mean(), atr) * 100.0
    minus_di = _safe_div(minus_dm.rolling(period).mean(), atr) * 100.0
    dx = _safe_div((plus_di - minus_di).abs(), plus_di + minus_di) * 100.0
    adx = dx.rolling(period).mean()
    return plus_di, minus_di, adx


def psy_series(close, period: int = 12) -> pd.Series:
    """PSY 逐日序列 = 窗口内上涨日占比 × 100。"""
    c = pd.Series(close).astype(float)
    up = (c.diff() > 0).astype(float)
    return up.rolling(period).sum() / period * 100.0


def vr_series(close, volume, period: int = 26) -> pd.Series:
    """VR 逐日序列 = (上涨量 + 平盘量/2) / (下跌量 + 平盘量/2) × 100。"""
    c = pd.Series(close).astype(float)
    v = pd.Series(volume).astype(float)
    diff = c.diff()
    rv = v.where(diff > 0, 0.0)
    bv = v.where(diff < 0, 0.0)
    cv = v.where(diff == 0, 0.0) / 2.0
    num = (rv + cv).rolling(period).sum()
    den = (bv + cv).rolling(period).sum()
    return _safe_div(num, den) * 100.0


# ============================================================
# 因子注册表
# ============================================================

@dataclass(frozen=True)
class FactorSpec:
    """一个可评估因子的元数据 + 计算函数。"""

    factor_id: str
    name: str
    category: str
    fn: Callable[[pd.DataFrame], pd.Series]
    description: str
    min_rows: int = MIN_FACTOR_ROWS


def _c(df: pd.DataFrame) -> pd.Series:
    return pd.Series(df["close"].values, index=df.index, dtype="float64")


def _bias(period: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        ma = _series_from(_sma(c.values, period), c.index)
        return _safe_div(c, ma) - 1.0
    return _fn


def _series_from(values, index) -> pd.Series:
    return pd.Series(np.asarray(values, dtype=float), index=index, dtype="float64")


def _ret(period: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        return c / c.shift(period) - 1.0
    return _fn


def _vol(period: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        return c.pct_change().rolling(period).std()
    return _fn


def _fn_ma_spread(fast: int, slow: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        f = _series_from(_sma(c.values, fast), c.index)
        s = _series_from(_sma(c.values, slow), c.index)
        return _safe_div(f, s) - 1.0
    return _fn


def _fn_ema_bias(period: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        e = _series_from(_ema(c.values, period), c.index)
        return _safe_div(c, e) - 1.0
    return _fn


def _fn_rsi(period: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        return _series_from(_rsi(c.values, period), c.index)
    return _fn


def _fn_kdj(i: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        k, d, j = _kdj(df["high"].values, df["low"].values, df["close"].values)
        return _series_from((k, d, j)[i], df.index)
    return _fn


def _fn_macd(i: int):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        dif, dea, hist = _macd_calc(c.values)
        # 除以收盘价做量纲归一：原始 DIF 与股价成正比，直接截面排名等于在排股价
        return _safe_div(_series_from((dif, dea, hist)[i], c.index), c)
    return _fn


def _fn_boll(kind: str):
    def _fn(df: pd.DataFrame) -> pd.Series:
        c = _c(df)
        upper, mid, lower = _bbands(c.values)
        u = _series_from(upper, c.index)
        m = _series_from(mid, c.index)
        lo = _series_from(lower, c.index)
        if kind == "pctb":
            return _safe_div(c - lo, u - lo)
        return _safe_div(u - lo, m)
    return _fn


def _fn_atr_pct(df: pd.DataFrame) -> pd.Series:
    c = _c(df)
    atr = atr_series(df["high"].values, df["low"].values, df["close"].values, 14)
    return _safe_div(atr, c)


def _fn_cci(df: pd.DataFrame) -> pd.Series:
    return cci_series(df["high"].values, df["low"].values, df["close"].values, 20)


def _fn_wr(df: pd.DataFrame) -> pd.Series:
    return wr_series(df["high"].values, df["low"].values, df["close"].values, 14)


def _fn_adx(df: pd.DataFrame) -> pd.Series:
    _, _, adx = dmi_series(df["high"].values, df["low"].values, df["close"].values, 14)
    return adx


def _fn_di_diff(df: pd.DataFrame) -> pd.Series:
    plus, minus, _ = dmi_series(df["high"].values, df["low"].values, df["close"].values, 14)
    return plus - minus


def _fn_psy(df: pd.DataFrame) -> pd.Series:
    return psy_series(df["close"].values, 12)


def _fn_vr(df: pd.DataFrame) -> pd.Series:
    return vr_series(df["close"].values, df["volume"].values, 26)


def _fn_vol_ratio(df: pd.DataFrame) -> pd.Series:
    v = pd.Series(df["volume"].values, index=df.index, dtype="float64")
    return _safe_div(v.rolling(5).mean(), v.rolling(20).mean())


def _fn_amount_ratio(df: pd.DataFrame) -> pd.Series:
    if "amount" not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    a = pd.Series(df["amount"].values, index=df.index, dtype="float64")
    return _safe_div(a, a.rolling(5).mean())


def _fn_obv_slope(df: pd.DataFrame) -> pd.Series:
    """OBV 20 日变化 /（20 日均量 × 20）→ 无量纲净流入强度。"""
    c = pd.Series(df["close"].values, index=df.index, dtype="float64")
    v = pd.Series(df["volume"].values, index=df.index, dtype="float64")
    obv = _series_from(_obv(c.values, v.values), c.index)
    scale = v.rolling(20).mean() * 20.0
    return _safe_div(obv - obv.shift(20), scale)


def _fn_close_position(df: pd.DataFrame) -> pd.Series:
    c = _c(df)
    hh = c.rolling(20).max()
    ll = c.rolling(20).min()
    return _safe_div(c - ll, hh - ll)


def _fn_price_range(df: pd.DataFrame) -> pd.Series:
    c = _c(df)
    hh = c.rolling(20).max()
    ll = c.rolling(20).min()
    return _safe_div(hh - ll, c)


_SPECS: List[FactorSpec] = [
    # ── 趋势 / 动量 ────────────────────────────────────────
    FactorSpec("ma_bias_5", "MA5 乖离率", "趋势", _bias(5), "收盘价相对 5 日均线的偏离，短期超买超卖"),
    FactorSpec("ma_bias_10", "MA10 乖离率", "趋势", _bias(10), "收盘价相对 10 日均线的偏离"),
    FactorSpec("ma_bias_20", "MA20 乖离率", "趋势", _bias(20), "收盘价相对 20 日均线的偏离"),
    FactorSpec("ma_bias_60", "MA60 乖离率", "趋势", _bias(60), "收盘价相对 60 日均线的偏离，中期位置", 65),
    FactorSpec("ema_bias_12", "EMA12 乖离率", "趋势", _fn_ema_bias(12), "收盘价相对 12 日 EMA 的偏离"),
    FactorSpec("ma_spread_5_20", "MA5/MA20 价差", "趋势", _fn_ma_spread(5, 20), "短期均线相对中期均线的位置（多头排列强度）"),
    FactorSpec("ma_spread_20_60", "MA20/MA60 价差", "趋势", _fn_ma_spread(20, 60), "中期均线相对长期均线的位置", 65),
    FactorSpec("ret_5", "5 日动量", "动量", _ret(5), "过去 5 个交易日累计收益"),
    FactorSpec("ret_10", "10 日动量", "动量", _ret(10), "过去 10 个交易日累计收益"),
    FactorSpec("ret_20", "20 日动量", "动量", _ret(20), "过去 20 个交易日累计收益（月度反转/动量主口径）"),
    FactorSpec("ret_60", "60 日动量", "动量", _ret(60), "过去 60 个交易日累计收益（季度动量）", 65),

    # ── 摆动指标 ───────────────────────────────────────────
    FactorSpec("rsi_6", "RSI(6)", "摆动", _fn_rsi(6), "6 日相对强弱，短周期超买超卖", 10),
    FactorSpec("rsi_14", "RSI(14)", "摆动", _fn_rsi(14), "14 日相对强弱（经典口径）"),
    FactorSpec("rsi_24", "RSI(24)", "摆动", _fn_rsi(24), "24 日相对强弱，中周期"),
    FactorSpec("kdj_k", "KDJ-K", "摆动", _fn_kdj(0), "随机指标 K 线"),
    FactorSpec("kdj_d", "KDJ-D", "摆动", _fn_kdj(1), "随机指标 D 线"),
    FactorSpec("kdj_j", "KDJ-J", "摆动", _fn_kdj(2), "随机指标 J 线（超买超卖最敏感）"),
    FactorSpec("wr_14", "威廉 %R(14)", "摆动", _fn_wr, "14 日威廉指标，负值越大越弱"),
    FactorSpec("cci_20", "CCI(20)", "摆动", _fn_cci, "顺势指标，衡量价格偏离统计平均的程度"),
    FactorSpec("psy_12", "PSY(12)", "摆动", _fn_psy, "12 日心理线，上涨日占比"),

    # ── MACD（除以收盘价归一） ─────────────────────────────
    FactorSpec("macd_dif", "MACD-DIF", "MACD", _fn_macd(0), "快慢 EMA 差（归一化）", 30),
    FactorSpec("macd_dea", "MACD-DEA", "MACD", _fn_macd(1), "DIF 的信号线（归一化）", 30),
    FactorSpec("macd_hist", "MACD 柱", "MACD", _fn_macd(2), "DIF−DEA 的 2 倍（归一化）", 30),

    # ── BOLL ───────────────────────────────────────────────
    FactorSpec("boll_pctb", "BOLL %B", "通道", _fn_boll("pctb"), "价格在布林带中的相对位置（0=下轨，1=上轨）"),
    FactorSpec("boll_bandwidth", "BOLL 带宽", "通道", _fn_boll("bandwidth"), "布林带宽度/中轨，波动收敛扩张"),

    # ── DMI ────────────────────────────────────────────────
    FactorSpec("dmi_adx", "ADX(14)", "趋势强度", _fn_adx, "趋势强度（不辨方向）", 30),
    FactorSpec("dmi_di_diff", "DI 差", "趋势强度", _fn_di_diff, "+DI − −DI，方向性趋势强度", 30),

    # ── 量能 ───────────────────────────────────────────────
    FactorSpec("vol_ratio_5_20", "量比(5/20)", "量能", _fn_vol_ratio, "5 日均量 / 20 日均量，放量程度"),
    FactorSpec("amount_ratio_5", "额比(1/5)", "量能", _fn_amount_ratio, "当日成交额 / 5 日均额"),
    FactorSpec("obv_slope_20", "OBV 斜率(20)", "量能", _fn_obv_slope, "20 日 OBV 变化归一化，资金净流入强度"),
    FactorSpec("vr_26", "VR(26)", "量能", _fn_vr, "26 日量比指标，上涨量与下跌量之比"),

    # ── 波动 / 位置 ────────────────────────────────────────
    FactorSpec("atr_pct", "ATR 占比", "波动", _fn_atr_pct, "14 日 ATR / 收盘价，波动幅度"),
    FactorSpec("volatility_20", "波动率(20)", "波动", _vol(20), "20 日收益率标准差"),
    FactorSpec("volatility_60", "波动率(60)", "波动", _vol(60), "60 日收益率标准差", 65),
    FactorSpec("close_position_20", "20 日价格位置", "位置", _fn_close_position, "(收盘−20 日最低)/(20 日最高−最低)"),
    FactorSpec("price_range_20", "20 日振幅", "位置", _fn_price_range, "(20 日最高−最低)/收盘"),
]

FACTOR_SPECS: Dict[str, FactorSpec] = {s.factor_id: s for s in _SPECS}
FACTOR_IDS: List[str] = [s.factor_id for s in _SPECS]
FACTOR_CATEGORIES: List[str] = list(dict.fromkeys(s.category for s in _SPECS))


# ============================================================
# 入口
# ============================================================

_REQUIRED_COLS = ("open", "high", "low", "close", "volume")


def validate_frame(df: pd.DataFrame) -> pd.DataFrame:
    """校验并按交易日升序排列；返回可直接计算的副本。"""
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"缺少必要列: {set(missing)}")
    out = df.copy()
    if "trade_date" in out.columns:
        out = out.sort_values("trade_date")
    out = out.reset_index(drop=True)
    for col in _REQUIRED_COLS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def compute_factors(df: pd.DataFrame,
                    factor_ids: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """计算因子面板。

    Args:
        df: 单票日线，需含 open/high/low/close/volume（可选 amount）。
            必须按交易日升序（``trade_date`` 存在时自动排序）。
        factor_ids: 要计算的因子；``None`` = 全部 36 个。

    Returns:
        DataFrame，index 与 ``df`` 对齐，列 = factor_id（float64）。
        预热期不足为 NaN，不填充。单因子异常时该列为 NaN 并继续（不中断整批）。
    """
    frame = validate_frame(df)
    ids = list(factor_ids) if factor_ids is not None else FACTOR_IDS
    unknown = [i for i in ids if i not in FACTOR_SPECS]
    if unknown:
        raise KeyError(f"未知因子: {unknown}")

    out = pd.DataFrame(index=frame.index)
    for fid in ids:
        spec = FACTOR_SPECS[fid]
        if len(frame) < spec.min_rows:
            out[fid] = np.nan
            continue
        try:
            series = spec.fn(frame)
            out[fid] = pd.Series(series).astype("float64").replace([np.inf, -np.inf], np.nan)
        except Exception:  # noqa: BLE001
            # 单因子失败不拖垮整批（PRD §8 错误状态）
            out[fid] = np.nan
    return out


def factor_meta() -> List[Dict[str, object]]:
    """因子清单（供报告与 API 展示）。"""
    return [
        {
            "factor_id": s.factor_id,
            "name": s.name,
            "category": s.category,
            "description": s.description,
            "min_rows": s.min_rows,
        }
        for s in _SPECS
    ]

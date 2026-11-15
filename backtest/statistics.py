"""回测统计工具（纯函数，无 IO）。

P1 设计：``docs/superpowers/specs/2026-10-15-backtest-p1-statistical-rigor-design.md``。

- PSR（Probabilistic Sharpe Ratio）/ Deflated Sharpe Ratio：Bailey & López de Prado（2012 / 2014）
- E[max SR]：多重检验下「纯运气」的期望最大夏普（Gumbel 近似）
- 日频收益统计（SR / skew / kurt）

⚠️ **口径约定**：PSR / DSR / E[max] 的夏普输入一律为 **per-period**（非年化）——
engine 的年化 ``sharpe_ratio`` 不能直接进公式；调用方负责换算（年化 ÷ √252 或
直接用本模块 ``daily_stats`` 的 ``sr_daily``）。

与本包 health.py 同风格：只有纯函数；数据读写留在 engine / 调用侧。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

TRADING_DAYS_PER_YEAR = 252
_EULER_GAMMA = 0.5772156649015329


def _nan_safe(values) -> list:
    """剔除 None / NaN / Inf，返回干净的 float 列表。"""
    out = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def daily_stats(returns: Sequence[float]) -> dict:
    """一次算齐日频统计：``{n, sr_daily, sr_annual, skew, kurt}``。

    - ``sr_daily`` = mean/std（**per-period，非年化**——PSR/DSR 的标准输入）
    - ``skew`` = 总体偏度；``kurt`` = 总体峰度（非超额，正态 = 3）
    - 样本不足（n < 3）或 std = 0 → 对应字段 None，**绝不造数**
    """
    rs = _nan_safe(returns)
    n = len(rs)
    out = {'n': n, 'sr_daily': None, 'sr_annual': None, 'skew': None, 'kurt': None}
    if n < 3:
        return out
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1)
    std = math.sqrt(var)
    if std <= 0:
        return out
    sr_d = mean / std
    # 零波动防护：恒定序列（含浮点噪声）会算出 1e6+ 量级的伪 SR——视为无定义
    if not math.isfinite(sr_d) or abs(sr_d) > 1e6:
        return out
    out['sr_daily'] = sr_d
    out['sr_annual'] = sr_d * math.sqrt(TRADING_DAYS_PER_YEAR)
    m3 = sum((r - mean) ** 3 for r in rs) / n
    m4 = sum((r - mean) ** 4 for r in rs) / n
    out['skew'] = m3 / std ** 3
    out['kurt'] = m4 / std ** 4
    return out


def calculate_yearly_stats(time_return, trades: Optional[Sequence[dict]] = None) -> dict:
    """按自然年聚合：``{year: {'ret'(%), 'sharpe'(年化), 'mdd'(%), 'trades'}}``。

    - ``ret``：该年复合收益（%）
    - ``sharpe``：该年日频 SR 年化（样本不足 → None）
    - ``mdd``：该年内最大回撤（**正值**，与 ``backtest_results.max_drawdown`` 同口径）
    - ``trades``：该年**完整交易对数**（只计 ``direction='buy'`` 的记录——与顶部
      「交易次数」指标同口径；``trades`` 缺省时省略该键；按 ``trade_date`` 年份归属）
    """
    buckets: dict = {}
    for date, ret in sorted((time_return or {}).items()):
        y = date.strftime('%Y') if hasattr(date, 'strftime') else str(date)[:4]
        buckets.setdefault(y, []).append(ret)
    out = {}
    for y, rets in buckets.items():
        cum = 1.0
        peak = 1.0
        mdd = 0.0
        for r in rets:
            cum *= (1.0 + (r or 0.0))
            peak = max(peak, cum)
            mdd = max(mdd, (peak - cum) / peak if peak > 0 else 0.0)
        d = daily_stats(rets)
        row = {
            'ret': round((cum - 1.0) * 100, 2),
            'sharpe': round(d['sr_annual'], 3) if d['sr_annual'] is not None else None,
            'mdd': round(mdd * 100, 2),
        }
        if trades is not None:
            # ⚠️ trade_records 是「买 + 卖」双记录；只数 buy 才是「交易对数」口径
            row['trades'] = sum(
                1 for t in trades
                if str((t or {}).get('trade_date', ''))[:4] == y
                and (t or {}).get('direction') == 'buy')
        out[y] = row
    return out


def _norm_cdf(x: float) -> float:
    """标准正态 CDF。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 有理近似，|误差| < 1.2e-9）。"""
    if p <= 0.0:
        return float('-inf')
    if p >= 1.0:
        return float('inf')
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def probabilistic_sharpe_ratio(sr: float, n: int, skew: float = 0.0,
                               kurt: float = 3.0,
                               target: float = 0.0) -> Optional[float]:
    """PSR：观测（per-period）夏普 > 阈值 ``target`` 的概率。

    ``PSR = Φ[ (SR − SR*)·√(n−1) / √(1 − γ₃·SR + (γ₄−1)/4·SR²) ]``

    - ``n < 2`` / sr 非有限 → None
    - 分母非正（极端 SR × 厚尾）→ clamp 到极小值（防 NaN）
    """
    if n is None or n < 2 or sr is None:
        return None
    try:
        sr = float(sr)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sr):
        return None
    g3 = float(skew) if skew is not None else 0.0
    g4 = float(kurt) if kurt is not None else 3.0
    denom2 = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    denom = math.sqrt(max(denom2, 1e-12))
    return _norm_cdf((sr - target) * math.sqrt(n - 1) / denom)


def expected_max_sharpe(n_trials: int, sr_std: float) -> Optional[float]:
    """N 次试验下「纯运气」的期望最大夏普（Bailey-LdP 2014 Gumbel 近似）。

    ``E[max] ≈ σ·[(1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))]``，γ = Euler-Mascheroni

    - ``n_trials < 2`` 或 ``sr_std <= 0`` → None（无可校正语义，**不造数**）
    - ``sr_std`` 与输出口径 = **per-period**（调用方负责换算，须与 PSR 的 sr 同口径）
    """
    if not n_trials or n_trials < 2 or sr_std is None:
        return None
    try:
        sigma = float(sr_std)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sigma * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def deflated_sharpe_ratio(sr, n, skew=None, kurt=None, n_trials=None,
                          sr_std=None) -> Optional[float]:
    """DSR = PSR(阈值 = E[max SR]|N)——「试了 N 次之后，这个夏普还算数吗」。

    输入口径统一 **per-period**；``skew`` / ``kurt`` 缺失按正态（0 / 3）处理
    （两者是 PSR 的小修正项）；``n_trials`` / ``sr_std`` 缺失 → None
    （**关键输入不造数**——「trial 方差不足，未校正」由调用方负责标注）。
    """
    target = expected_max_sharpe(n_trials, sr_std) if (n_trials and sr_std) else None
    if target is None:
        return None
    return probabilistic_sharpe_ratio(
        sr, n,
        skew if skew is not None else 0.0,
        kurt if kurt is not None else 3.0,
        target)

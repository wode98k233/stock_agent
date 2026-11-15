"""因子评估引擎：截面 IC / 分层回测 / IC 衰减 / 换手（纯函数，无 IO）。

口径（与 docs/superpowers/specs/2026-10-16-factor-eval-phase1-research.md §5.4 锁定一致）
--------------------------------------------------------------------------------------
- **Q1 = 因子值最小，Q5 = 因子值最大**（升序；聚宽相反，报告必须显式声明）
- **多空 = Q5 − Q1**（做多因子值最大组）；标签中性，接口不承载「多头/空头」语义
- **前向收益** = ``close[t+h] / close[t] − 1``，末 h 行为 NaN（不入统计）
- **防前视**：因子在 t 日收盘可得，但 ``lag=1``（默认）把因子后移一天，与
  ``t+1 → t+1+h`` 的收益配对——即「次日建仓」，符合 A 股收盘后才知道信号的事实
- **缺失一律 None，不拿 0 冒充**；0 是有经济含义的因子值/收益

为什么不用 Alphalens
--------------------
0.4.0（2020-04-27）停更且与 pandas 3.0.2 结构性不兼容（实测于 ADR-007）。
本模块只实现评估算法面（~10 个纯函数），口径可控、零新依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from backtest.factor_lib import (
    FACTOR_IDS,
    FACTOR_SPECS,
    compute_factors,
    validate_frame,
)
from backtest.statistics import (
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)

TRADING_DAYS_PER_YEAR = 242


# ============================================================
# 配置
# ============================================================

@dataclass(frozen=True)
class EvalConfig:
    """评估参数。所有口径都在这里，便于 ``params`` 回显与复现。"""

    ret_period: int = 5          # 前向收益周期（交易日）
    quantiles: int = 5
    lag: int = 1                 # 因子滞后（1 = 次日建仓；0 = 当日收盘口径）
    min_names: int = 30          # 单日截面最少标的数
    ic_method: str = "spearman"  # spearman（主）/ pearson（对照）
    ic_roll: int = 20            # IC 滚动平滑（仅展示）
    # ── A 股数据卫生层 ──
    exclude_suspended: bool = True   # volume <= 0 视为停牌/占位行
    exclude_limit: bool = True       # 观测日触板不进截面（当日无法成交）
    limit_pct: float = 0.10          # 主板涨跌停比例
    limit_tol: float = 0.005         # 容差（9.5% 起算触板）
    min_history: int = 60            # 单票最少历史行数（预热 + 次新过滤的近似）
    # ── 争议口径（首期固定，必须回显） ──
    cost_bps: float = 0.0
    neutralize: str = "none"
    rebalance: str = "period"

    def to_params(self) -> dict:
        return {
            "ret_period": self.ret_period,
            "ic_roll": self.ic_roll,
            "quantiles": self.quantiles,
            "lag": self.lag,
            "ic_method": self.ic_method,
            "min_names": self.min_names,
            "cost_bps": self.cost_bps,
            "neutralize": self.neutralize,
            "rebalance": self.rebalance,
        }


# ============================================================
# 面板构建
# ============================================================

_PANEL_COLS = ("trade_date", "code", "open", "high", "low", "close",
               "pct_change", "volume", "amount")


def build_panel(frames: Dict[str, pd.DataFrame],
                factor_ids: Optional[Sequence[str]] = None,
                min_history: int = 60,
                meta_map: Optional[Dict[str, dict]] = None) -> pd.DataFrame:
    """把多票日线 + 因子值拼成横截面长表。

    Args:
        frames: ``{code: DataFrame}``，需含 trade_date/open/high/low/close/volume。
        factor_ids: 要计算的因子，None = 全部。
        min_history: 行数不足的票整票跳过。
        meta_map: ``{code: {industry, size}}``（中性化用；size = 自由流通市值代理）。

    Returns:
        长表，列为 trade_date / code / OHLC / pct_change / volume / amount +
        ``f_<factor_id>`` + ``industry`` / ``size``；按 (code, trade_date) 升序。
    """
    ids = list(factor_ids) if factor_ids is not None else FACTOR_IDS
    chunks: List[pd.DataFrame] = []

    for code, raw in frames.items():
        if raw is None or len(raw) == 0:
            continue
        try:
            frame = validate_frame(raw)
        except ValueError:
            continue
        if len(frame) < max(min_history, 20):
            continue

        fac = compute_factors(frame, ids)
        base = frame[["trade_date", "open", "high", "low", "close",
                      "volume", "amount"]].copy() if "amount" in frame.columns else \
            frame[["trade_date", "open", "high", "low", "close", "volume"]].copy()
        if "amount" not in base.columns:
            base["amount"] = np.nan

        if "pct_change" in frame.columns:
            base["pct_change"] = pd.to_numeric(frame["pct_change"], errors="coerce")
        else:
            base["pct_change"] = (base["close"].pct_change() * 100.0)

        base["code"] = code
        info = (meta_map or {}).get(code) or {}
        base["industry"] = info.get("industry")
        base["size"] = info.get("size")
        for fid in ids:
            base[f"f_{fid}"] = fac[fid].to_numpy()
        chunks.append(base)

    if not chunks:
        return pd.DataFrame(columns=list(_PANEL_COLS))

    panel = pd.concat(chunks, ignore_index=True)
    panel["trade_date"] = panel["trade_date"].astype(str)
    panel = panel.sort_values(["code", "trade_date"]).reset_index(drop=True)
    return panel


def add_forward_returns(panel: pd.DataFrame, period: int = 5) -> pd.DataFrame:
    """按票计算前向收益 ``close[t+h]/close[t] − 1``（末 h 行 NaN）。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    out = panel.sort_values(["code", "trade_date"]).copy()
    g = out.groupby("code", sort=False)["close"]
    out["fwd_ret"] = g.shift(-period) / out["close"] - 1.0
    # 同一票的 close 缺失会传染，统一清成 NaN
    out.loc[~np.isfinite(out["fwd_ret"].to_numpy(dtype=float)), "fwd_ret"] = np.nan
    return out


def apply_hygiene(panel: pd.DataFrame, cfg: EvalConfig) -> pd.DataFrame:
    """A 股数据卫生层：剔除当日不可交易的观测。

    - **停牌**：``volume <= 0``（库里的占位行；真停牌多为缺行，由 bar 缺失自然跳过）
    - **涨跌停**：观测日 ``|pct_change| >= limit_pct−tol`` 不进截面——当日无法按
      收盘价建仓，纳入会让 IC 高估
    - **次新**：用 ``min_history``（行数不足）近似；``list_date`` 未进面板，
      精确口径待后续接入 stock_info
    - **ST**：``is_st`` 是死列（项目 §1.3 已记录），不做伪过滤，报告里标注未覆盖
    """
    if panel.empty:
        return panel
    out = panel
    mask = pd.Series(True, index=out.index)

    if cfg.exclude_suspended:
        mask &= pd.to_numeric(out["volume"], errors="coerce").fillna(0) > 0

    if cfg.exclude_limit and "pct_change" in out.columns:
        thresh = (cfg.limit_pct - cfg.limit_tol) * 100.0   # 9.5（百分点）
        pc = pd.to_numeric(out["pct_change"], errors="coerce").abs()
        mask &= ~(pc >= thresh)

    return out[mask].reset_index(drop=True)


# ============================================================
# 中性化（行业 + 市值）
# ============================================================

def neutralize_factor(panel: pd.DataFrame,
                      factor_col: str,
                      cfg: EvalConfig,
                      return_diagnostics: bool = False):
    """横截面中性化：对「行业哑变量 + log(size)」回归取残差。

    做法（每个交易日独立回归）::

        factor_i = α + β·log(size_i) + Σ γ_k·1[行业_i = k] + ε_i
        neutral_i = ε_i

    实现用 **Frisch–Waugh–Lovell 等价变换**：先把因子与 log(size) 各自在行业内
    de-mean，再对二维做回归。数学上与「67 个行业哑变量 + 截距」的完整 OLS 完全
    等价，但每期只需一次二维最小二乘（实测 36 因子从 250s → 约 40s）。

    口径说明
    --------
    - ``size`` 用**成分权重**代理自由流通市值（真实市值接口被网络拦截，见
      scripts/ops/enrich_universe_meta.py 的说明）。回归只用到 log(size) 的
      相对差异，整体乘常数不改变残差。
    - 行业缺失记为「未知」独立一组，不丢样本（丢样本会引入选择偏差）。
    - 单日有效样本 < 10 时，该日保留原值（不造残差）。
    - ``cfg.neutralize='none'`` 时直接返回原列。
    """
    if cfg.neutralize == "none" or factor_col not in panel.columns:
        return (panel[factor_col], {"mode": "none"}) if return_diagnostics \
            else panel[factor_col]

    use_ind = cfg.neutralize in ("industry", "both")
    use_size = cfg.neutralize in ("size", "both")

    ind = panel["industry"].astype(object).where(panel["industry"].notna(), "未知") \
        if "industry" in panel.columns else pd.Series(["未知"] * len(panel), index=panel.index)
    cat = pd.Categorical(ind)
    codes = np.asarray(cat.codes)
    n_ind = len(cat.categories)

    size = pd.to_numeric(panel["size"], errors="coerce") if "size" in panel.columns \
        else pd.Series(np.nan, index=panel.index)
    log_size = np.log(size.where(size > 0))
    log_arr = log_size.to_numpy(dtype=float)

    y_all = panel[factor_col].to_numpy(dtype=float)
    out = np.full(len(panel), np.nan, dtype=float)

    dates = panel["trade_date"].to_numpy()
    order = np.argsort(dates, kind="stable")
    uniq, starts = np.unique(dates[order], return_index=True)
    bounds = list(starts) + [len(order)]

    resolved = 0
    for i in range(len(uniq)):
        idx = order[bounds[i]:bounds[i + 1]]
        y = y_all[idx]
        mask = np.isfinite(y)
        if use_size:
            mask &= np.isfinite(log_arr[idx])
        n = int(mask.sum())
        if n < 10:
            out[idx] = y          # 样本不足：保留原值（不造残差）
            continue
        gpos = idx[mask]
        yv = y[mask]
        g = codes[gpos]
        xv = log_arr[gpos] if use_size else None

        if use_ind:
            cnt = np.bincount(g, minlength=n_ind).astype(float)
            safe = np.maximum(cnt, 1.0)
            mean_y = np.bincount(g, weights=yv, minlength=n_ind) / safe
            y_tilde = yv - mean_y[g]
            if use_size:
                mean_x = np.bincount(g, weights=xv, minlength=n_ind) / safe
                x_tilde = xv - mean_x[g]
        else:
            y_tilde = yv - yv.mean()
            x_tilde = (xv - xv.mean()) if use_size else None

        if use_size and x_tilde is not None:
            denom = float((x_tilde ** 2).sum())
            beta = float((x_tilde * y_tilde).sum() / denom) if denom > 1e-12 else 0.0
            resid = y_tilde - beta * x_tilde
        else:
            resid = y_tilde

        out[gpos] = resid
        resolved += 1
        # 被 size 缺失剔掉的行保持 NaN（中性化后无法定义），不混入原始值

    series = pd.Series(out, index=panel.index, dtype="float64")
    if return_diagnostics:
        return series, {
            "mode": cfg.neutralize,
            "dates_regressed": resolved,
            "industry_groups": int(n_ind),
            "size_coverage": float(np.isfinite(log_arr).mean()) if len(panel) else 0.0,
        }
    return series


# ============================================================
# IC
# ============================================================

def _rank_within_date(df: pd.DataFrame, col: str) -> pd.Series:
    return df.groupby("trade_date")[col].rank(method="average")


def compute_ic_series(panel: pd.DataFrame,
                      factor_col: str,
                      ret_col: str = "fwd_ret",
                      method: str = "spearman",
                      min_names: int = 30) -> pd.Series:
    """逐日截面 IC 序列（向量化实现，不逐日 apply）。

    Spearman = 先按日排名再算 Pearson——避免 scipy 依赖，也避免逐日循环。
    单日有效标的 < ``min_names`` 时跳过（不产出噪声值）。
    """
    if factor_col not in panel.columns or ret_col not in panel.columns:
        return pd.Series(dtype="float64")

    d = panel[["trade_date", factor_col, ret_col]].dropna()
    if d.empty:
        return pd.Series(dtype="float64")

    counts = d.groupby("trade_date")[factor_col].transform("size")
    d = d[counts >= min_names]
    if d.empty:
        return pd.Series(dtype="float64")

    if method == "spearman":
        x = _rank_within_date(d, factor_col)
        y = _rank_within_date(d, ret_col)
    else:
        x = d[factor_col].astype(float)
        y = d[ret_col].astype(float)

    tmp = pd.DataFrame({"trade_date": d["trade_date"], "x": x, "y": y})
    g = tmp.groupby("trade_date")
    mx = g["x"].transform("mean")
    my = g["y"].transform("mean")
    tmp["dx"] = tmp["x"] - mx
    tmp["dy"] = tmp["y"] - my
    tmp["xy"] = tmp["dx"] * tmp["dy"]
    tmp["xx"] = tmp["dx"] ** 2
    tmp["yy"] = tmp["dy"] ** 2

    agg = tmp.groupby("trade_date")[["xy", "xx", "yy"]].sum()
    denom = np.sqrt(agg["xx"] * agg["yy"])
    ic = (agg["xy"] / denom).replace([np.inf, -np.inf], np.nan).dropna()
    ic.index = [str(i) for i in ic.index]
    ic.name = "ic"
    return ic.sort_index()


def summarize_ic(ic: pd.Series, periods_per_year: Optional[float] = None) -> dict:
    """IC 摘要：均值 / 标准差 / ICIR / t 值 / 胜率。

    ``t = ICIR × √n``（iid 假设；日频 IC 存在自相关，t 值偏乐观——报告里注明）。
    """
    n = int(len(ic))
    out = {
        "n_periods": n,
        "ic_mean": None, "ic_std": None, "icir": None,
        "t_value": None, "win_rate": None, "ic_abs_mean": None,
        "ic_annual_icir": None,
    }
    if n < 3:
        return out
    arr = ic.to_numpy(dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 3:
        return out
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    out["n_periods"] = int(len(arr))
    out["ic_mean"] = mean
    out["ic_std"] = std
    out["ic_abs_mean"] = float(np.abs(arr).mean())
    out["win_rate"] = float((arr > 0).mean())
    if std > 1e-12:
        icir = mean / std
        out["icir"] = icir
        out["t_value"] = icir * float(np.sqrt(len(arr)))
        if periods_per_year:
            out["ic_annual_icir"] = icir * float(np.sqrt(periods_per_year))
    return out


# ============================================================
# 分层
# ============================================================

def assign_quantiles(panel: pd.DataFrame,
                     factor_col: str,
                     quantiles: int = 5,
                     min_names: int = 30) -> pd.DataFrame:
    """给每行打分层标签（**Q1 = 因子值最小**，升序）。

    用 ``rank(method='first')`` 再切分，避免大量并列值导致 ``qcut`` 报
    「Bin edges must be unique」。
    """
    d = panel[["trade_date", "code", factor_col]].dropna().copy()
    if d.empty:
        return d.assign(quantile=pd.Series(dtype=int))
    counts = d.groupby("trade_date")[factor_col].transform("size")
    d = d[counts >= min_names]
    if d.empty:
        return d.assign(quantile=pd.Series(dtype=int))
    d["quantile"] = d.groupby("trade_date")[factor_col].transform(
        lambda s: pd.qcut(s.rank(method="first"), quantiles, labels=False) + 1
    ).astype(int)
    return d


def compute_quantile_returns(panel: pd.DataFrame,
                             factor_col: str,
                             ret_col: str = "fwd_ret",
                             quantiles: int = 5,
                             min_names: int = 30) -> pd.DataFrame:
    """各分层每期的等权平均收益（date × Q1..Qn）。"""
    d = panel[["trade_date", "code", factor_col, ret_col]].dropna()
    if d.empty:
        return pd.DataFrame()
    counts = d.groupby("trade_date")[factor_col].transform("size")
    d = d[counts >= min_names]
    if d.empty:
        return pd.DataFrame()
    d = d.copy()
    d["quantile"] = d.groupby("trade_date")[factor_col].transform(
        lambda s: pd.qcut(s.rank(method="first"), quantiles, labels=False) + 1
    ).astype(int)
    qret = d.groupby(["trade_date", "quantile"])[ret_col].mean().unstack("quantile")
    qret.columns = [f"Q{int(c)}" for c in qret.columns]
    qret.index = [str(i) for i in qret.index]
    return qret.sort_index()


def _nav(returns: pd.Series) -> np.ndarray:
    """净值曲线（缺失期视为空仓 0 收益——净值本身不能断线）。"""
    r = returns.fillna(0.0).to_numpy(dtype=float)
    return np.cumprod(1.0 + r)


def compute_quantile_equity(qret: pd.DataFrame,
                            quantiles: int = 5) -> dict:
    """分层净值 + 多空（Q5−Q1）+ 等权基准。

    Returns:
        ``{dates, series:[{name,data}], long_short:{name,data}, benchmark:{...}}``
        —— 直接可喂 ECharts（``dates`` 与每条 ``data`` 等长）。
    """
    if qret.empty:
        return {"dates": [], "series": [], "long_short": None, "benchmark": None}

    dates = list(qret.index)
    series = []
    for i in range(1, quantiles + 1):
        col = f"Q{i}"
        if col in qret.columns:
            series.append({
                "name": col,
                "data": [round(float(x), 6) for x in _nav(qret[col])],
            })

    ls = None
    # 多空 = 最高分位 − 最低分位。**不能硬编码 Q5**：分层数由请求决定（3/5/10），
    # 写死 Q5−Q1 会让 quantiles≠5 时多空夏普/DSR 全部算不出来，页面显示「—」。
    top = f"Q{quantiles}"
    if top in qret.columns and "Q1" in qret.columns:
        spread = qret[top] - qret["Q1"]
        ls = {
            "name": f"{top}−Q1 多空",
            "data": [round(float(x), 6) for x in _nav(spread)],
        }

    bench = {
        "name": "等权基准",
        "data": [round(float(x), 6) for x in _nav(qret.mean(axis=1))],
    }
    return {"dates": dates, "series": series, "long_short": ls, "benchmark": bench}


def compute_quantile_stats(qret: pd.DataFrame,
                           ret_period: int = 5) -> Dict[str, dict]:
    """各分层统计：累计/年化收益、夏普、最大回撤、胜率。"""
    if qret.empty:
        return {}
    periods_per_year = TRADING_DAYS_PER_YEAR / max(ret_period, 1)
    out: Dict[str, dict] = {}
    for col in qret.columns:
        r = qret[col].dropna()
        if len(r) < 3:
            out[col] = {"total_return": None, "annual_return": None,
                        "sharpe": None, "max_drawdown": None,
                        "win_rate": None, "n_periods": int(len(r))}
            continue
        nav = _nav(r)
        total = float(nav[-1] - 1.0)
        ann = (1.0 + total) ** (periods_per_year / len(r)) - 1.0 if total > -1 else -1.0
        std = float(r.std(ddof=1))
        sharpe = float(r.mean() / std * np.sqrt(periods_per_year)) if std > 1e-12 else None
        peak = np.maximum.accumulate(nav)
        mdd = float(((peak - nav) / peak).max()) if len(nav) else None
        out[col] = {
            "total_return": total,
            "annual_return": ann,
            "sharpe": sharpe,
            "max_drawdown": mdd,
            "win_rate": float((r > 0).mean()),
            "n_periods": int(len(r)),
        }
    return out


def compute_monotonicity(qret: pd.DataFrame) -> dict:
    """分层单调性：分位序号与平均收益的 Spearman 相关。"""
    if qret.empty:
        return {"spearman": None, "mean_returns": {}, "is_monotonic": None}
    means = qret.mean(axis=0).dropna()
    if len(means) < 3:
        return {"spearman": None, "mean_returns": {k: float(v) for k, v in means.items()},
                "is_monotonic": None}
    x = np.arange(1, len(means) + 1, dtype=float)
    y = means.to_numpy(dtype=float)
    # ⚠️ pandas 3.0 的 to_numpy() 可能返回只读视图，`sx -= mean` 会抛
    # "output array is read-only" —— 必须显式重新赋值而不是原地运算。
    sx = pd.Series(x).rank().to_numpy(dtype=float)
    sy = pd.Series(y).rank().to_numpy(dtype=float)
    sx = sx - sx.mean()
    sy = sy - sy.mean()
    denom = np.sqrt((sx ** 2).sum() * (sy ** 2).sum())
    rho = float((sx * sy).sum() / denom) if denom > 0 else None
    return {
        "spearman": rho,
        "mean_returns": {str(k): float(v) for k, v in means.items()},
        "is_monotonic": bool(rho is not None and abs(rho) > 0.9),
    }


def compute_turnover(panel: pd.DataFrame,
                     factor_col: str,
                     quantiles: int = 5,
                     min_names: int = 30) -> dict:
    """分层换手率：同一分层相邻两期成分变化比例（单边）。"""
    d = assign_quantiles(panel, factor_col, quantiles, min_names)
    if d.empty:
        return {"dates": [], "series": []}
    mat = d.pivot(index="trade_date", columns="code", values="quantile").sort_index()
    dates = [str(i) for i in mat.index]
    series = []
    for i in range(1, quantiles + 1):
        memb = (mat == i)
        prev = memb.shift(1)
        inter = (memb & prev).sum(axis=1)
        size_prev = prev.sum(axis=1)
        size_now = memb.sum(axis=1)
        # 分母取前后两期成分数的均值：只用当期会在「成分数变化」时把换手率算到 >1
        # （例如 5 出 5 进、组内仍 8 只 → 10/8=1.25）。均值分母天然有界于 [0,1]。
        denom = (size_prev + size_now) / 2.0
        to = (1.0 - inter / denom.replace(0, np.nan)).iloc[1:]
        series.append({
            "name": f"Q{i}",
            "data": [None if pd.isna(v) else round(float(v), 4) for v in to],
        })
    return {"dates": dates[1:], "series": series}


# ============================================================
# IC 衰减
# ============================================================

def compute_ic_decay(panel: pd.DataFrame,
                     factor_col: str,
                     cfg: EvalConfig,
                     periods: Sequence[int] = (1, 5, 10, 20)) -> dict:
    """不同持有期的 IC 衰减（回答「信号能管几天」）。"""
    rows = []
    for p in periods:
        pnl = add_forward_returns(panel, p)
        if cfg.lag:
            pnl = pnl.copy()
            pnl[factor_col] = pnl.groupby("code", sort=False)[factor_col].shift(cfg.lag)
        ic = compute_ic_series(pnl, factor_col, "fwd_ret", cfg.ic_method, cfg.min_names)
        s = summarize_ic(ic)
        rows.append({
            "period": int(p),
            "ic_mean": s["ic_mean"],
            "icir": s["icir"],
            "t_value": s["t_value"],
            "win_rate": s["win_rate"],
            "n_periods": s["n_periods"],
        })
    return {"periods": [r["period"] for r in rows], "rows": rows}


# ============================================================
# 单因子评估
# ============================================================

def _monthly_ic_matrix(ic: pd.Series) -> dict:
    """月度 IC 矩阵（年 × 月），供热力图。"""
    if ic.empty:
        return {"years": [], "months": list(range(1, 13)), "values": []}
    idx = pd.to_datetime(pd.Index(ic.index), errors="coerce")
    s = pd.Series(ic.to_numpy(dtype=float), index=idx).dropna()
    if s.empty:
        return {"years": [], "months": list(range(1, 13)), "values": []}
    g = s.groupby([s.index.year, s.index.month]).mean()
    years = sorted({int(y) for y, _ in g.index})
    values = []
    for y in years:
        row = []
        for m in range(1, 13):
            v = g.get((y, m))
            row.append(None if v is None or pd.isna(v) else round(float(v), 6))
        values.append(row)
    return {"years": years, "months": list(range(1, 13)), "values": values}


def _histogram(values: np.ndarray, bins: int = 30) -> dict:
    if len(values) == 0:
        return {"edges": [], "counts": []}
    counts, edges = np.histogram(values, bins=bins)
    return {
        "edges": [round(float(e), 6) for e in edges],
        "counts": [int(c) for c in counts],
    }


def _ls_returns(qret: pd.DataFrame, quantiles: int = 5) -> pd.Series:
    """多空组合每期收益 = 最高分位 − Q1。分位数由调用方给，不写死 Q5。"""
    top = f"Q{quantiles}"
    if top in qret.columns and "Q1" in qret.columns:
        return (qret[top] - qret["Q1"]).dropna()
    return pd.Series(dtype="float64")


def evaluate_factor(panel: pd.DataFrame,
                    factor_id: str,
                    cfg: EvalConfig = EvalConfig()) -> dict:
    """单因子完整评估。返回结构对齐调研文档 §5.4 的接口契约。"""
    if factor_id not in FACTOR_SPECS:
        raise KeyError(f"未知因子: {factor_id}")
    spec = FACTOR_SPECS[factor_id]
    fcol = f"f_{factor_id}"

    base = add_forward_returns(panel, cfg.ret_period)
    if cfg.lag:
        base = base.copy()
        base[fcol] = base.groupby("code", sort=False)[fcol].shift(cfg.lag)
    clean = apply_hygiene(base, cfg)

    # 行业/市值中性化：用回归残差替换因子值（PRD F9）
    neut_diag = {"mode": "none"}
    if cfg.neutralize != "none":
        clean = clean.copy()
        clean[fcol], neut_diag = neutralize_factor(clean, fcol, cfg,
                                                  return_diagnostics=True)

    # 口径命名（行业惯例，勿混）：
    #   IC     = Pearson 相关（受极端值影响大，作对照）
    #   RankIC = Spearman 秩相关（**主口径**：A 股小市值/异常值多，秩相关更稳）
    # 主口径决定 ICIR / t / 胜率 / 排序，Pearson 只作展示对照。
    ic_rank = compute_ic_series(clean, fcol, "fwd_ret", "spearman", cfg.min_names)
    ic_pearson = compute_ic_series(clean, fcol, "fwd_ret", "pearson", cfg.min_names)
    if cfg.ic_method == "pearson":
        primary = ic_pearson
    else:
        primary = ic_rank
    summary = summarize_ic(primary, TRADING_DAYS_PER_YEAR / max(cfg.ret_period, 1))

    qret = compute_quantile_returns(clean, fcol, "fwd_ret", cfg.quantiles, cfg.min_names)
    equity = compute_quantile_equity(qret, cfg.quantiles)
    qstats = compute_quantile_stats(qret, cfg.ret_period)
    mono = compute_monotonicity(qret)
    turnover = compute_turnover(clean, fcol, cfg.quantiles, cfg.min_names)
    decay = compute_ic_decay(panel, fcol, cfg)

    # 显著性：以「多空组合」的 per-period 夏普为主口径（可交易的那条线）
    ls = _ls_returns(qret, cfg.quantiles)
    # 多空年化必须来自**价差序列**，不能拿最高分位那一层的年化充数。
    # 旧实现取 qstats["Q5"].annual_return，会出现「年化 +18.9% 但夏普 −0.13」
    # 这种自相矛盾的展示（2026-10-17 用户截图暴露）。
    ls_stats = compute_quantile_stats(pd.DataFrame({"LS": ls}), cfg.ret_period).get("LS", {}) \
        if len(ls) >= 3 else {}
    sig = {"psr": None, "dsr": None, "e_max_sharpe": None,
           "n_trials": None, "long_short_sharpe": None,
           "sr_std_source": None}
    if len(ls) >= 3:
        std = float(ls.std(ddof=1))
        if std > 1e-12:
            sr = float(ls.mean() / std)
            skew = float(ls.skew()) if len(ls) > 2 else 0.0
            kurt = float(ls.kurtosis() + 3.0) if len(ls) > 3 else 3.0
            sig["long_short_sharpe"] = sr
            sig["psr"] = probabilistic_sharpe_ratio(sr, len(ls), skew, kurt, 0.0)

    roll = primary.rolling(cfg.ic_roll, min_periods=max(2, cfg.ic_roll // 3)).mean() \
        if len(primary) else pd.Series(dtype=float)
    dates = list(primary.index)

    def _align(s: pd.Series) -> list:
        return [None if pd.isna(v) else round(float(v), 6)
                for v in s.reindex(primary.index).to_numpy()]

    return {
        "factor_id": factor_id,
        "name": spec.name,
        "category": spec.category,
        "description": spec.description,
        "kpi": {
            "ic_mean": (float(ic_pearson.mean()) if len(ic_pearson) else None),
            "rank_ic_mean": (float(ic_rank.mean()) if len(ic_rank) else None),
            "icir": summary["icir"],
            "ic_win_rate": summary["win_rate"],
            "t_value": summary["t_value"],
            "n_periods": summary["n_periods"],
            "long_short_annual": ls_stats.get("annual_return"),
            "long_short_sharpe": sig["long_short_sharpe"],
            "monotonicity": mono["spearman"],
        },
        "ic_series": {
            "dates": dates,
            "series": [
                {"name": "IC（Pearson）", "data": _align(ic_pearson)},
                {"name": "RankIC（Spearman）", "data": [round(float(v), 6)
                                                        for v in primary.reindex(primary.index).to_numpy()]},
                {"name": f"RankIC {cfg.ic_roll} 期滚动均值",
                 "data": [None if pd.isna(v) else round(float(v), 6) for v in roll.to_numpy()]},
            ],
        },
        "ic_hist": _histogram(primary.to_numpy(dtype=float)),
        "ic_monthly_matrix": _monthly_ic_matrix(primary),
        "quantile_returns": {
            "dates": list(qret.index),
            "series": [
                {"name": col, "data": [None if pd.isna(v) else round(float(v), 6)
                                       for v in qret[col].to_numpy()]}
                for col in qret.columns
            ],
        },
        "quantile_equity": equity,
        "quantile_stats": qstats,
        "ic_decay": decay,
        "turnover": turnover,
        "monotonicity": mono,
        "significance": sig,
        "neutralization": neut_diag,
        "coverage": {
            "n_obs": int(len(clean)),
            "n_codes": int(clean["code"].nunique()) if not clean.empty else 0,
            "n_dates": int(clean["trade_date"].nunique()) if not clean.empty else 0,
            "ic_periods": int(len(primary)),
        },
    }


# ============================================================
# 批量评估（含多重检验校正）
# ============================================================

class FactorRunCancelled(RuntimeError):
    """评估被用户取消：progress 回调抛出本异常即可中断整批（不能被当成单因子失败吞掉）。"""


def _rank_key(item: dict) -> float:
    """排序主口径 = |RankIC|（Spearman，抗极端值）。"""
    ic = item.get("kpi", {}).get("rank_ic_mean")
    return abs(ic) if isinstance(ic, (int, float)) else -1.0


def evaluate_factors(panel: pd.DataFrame,
                     factor_ids: Optional[Sequence[str]] = None,
                     cfg: EvalConfig = EvalConfig(),
                     meta: Optional[dict] = None,
                     progress: Optional[Callable[[str, int, int, str], None]] = None) -> dict:
    """批量评估 + 横截面排序 + DSR 多重检验校正。

    DSR 的 ``n_trials`` = 本批因子数，``sr_std`` = 各因子多空夏普的标准差
    （**真实数据算出来，不估计**）——这正是「因子动物园」要防的：
    试了 N 个因子，最好的那个有多少是运气。

    ``progress(phase, index, total, label)``：每算完一个因子回调一次，
    供 SSE 推送真实进度；回调抛 :class:`FactorRunCancelled` 可中断整批。
    """
    ids = list(factor_ids) if factor_ids is not None else FACTOR_IDS
    results: List[dict] = []
    errors: List[dict] = []
    total = len(ids)

    for k, fid in enumerate(ids, 1):
        if progress:
            try:
                progress("evaluate", k, total, FACTOR_SPECS[fid].name)
            except FactorRunCancelled:
                raise
            except Exception:  # noqa: BLE001  进度回调绝不能打断评估
                pass
        try:
            results.append(evaluate_factor(panel, fid, cfg))
        except FactorRunCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append({"factor_id": fid, "error": f"{type(e).__name__}: {e}"})

    # ── 多重检验校正 ──
    srs = [r["significance"]["long_short_sharpe"] for r in results
           if r["significance"]["long_short_sharpe"] is not None]
    n_trials = len(srs)
    sr_std = float(np.std(srs, ddof=1)) if n_trials >= 2 else None
    for r in results:
        sig = r["significance"]
        sig["n_trials"] = n_trials if n_trials >= 2 else None
        sig["sr_std"] = round(sr_std, 6) if sr_std else None
        sig["sr_std_source"] = "batch" if sr_std else None
        if sig["long_short_sharpe"] is not None and n_trials >= 2 and sr_std:
            q = r["quantile_returns"]
            n_obs = len(q["dates"]) if q["dates"] else 0
            sig["dsr"] = deflated_sharpe_ratio(
                sig["long_short_sharpe"], n_obs,
                skew=None, kurt=None,
                n_trials=n_trials, sr_std=sr_std)
            from backtest.statistics import expected_max_sharpe
            sig["e_max_sharpe"] = expected_max_sharpe(n_trials, sr_std)

    results.sort(key=_rank_key, reverse=True)

    ranking = []
    for rank, r in enumerate(results, 1):
        k = r["kpi"]
        ranking.append({
            "rank": rank,
            "factor_id": r["factor_id"],
            "name": r["name"],
            "category": r["category"],
            "ic_mean": k["ic_mean"],
            "rank_ic_mean": k["rank_ic_mean"],
            "icir": k["icir"],
            "t_value": k["t_value"],
            "ic_win_rate": k["ic_win_rate"],
            "monotonicity": k["monotonicity"],
            "long_short_annual": k["long_short_annual"],
            "long_short_sharpe": k["long_short_sharpe"],
            "psr": r["significance"]["psr"],
            "dsr": r["significance"]["dsr"],
            "n_periods": k["n_periods"],
        })

    payload = {
        "ranking": ranking,
        "factors": {r["factor_id"]: r for r in results},
        "errors": errors,
        "params": cfg.to_params(),
        "significance": {
            "n_trials": n_trials if n_trials >= 2 else None,
            "sr_std": round(sr_std, 6) if sr_std else None,
            "method": "deflated_sharpe (Bailey & López de Prado)",
        },
        "coverage": {
            "n_codes": int(panel["code"].nunique()) if not panel.empty else 0,
            "n_dates": int(panel["trade_date"].nunique()) if not panel.empty else 0,
            "n_obs": int(len(panel)),
            "evaluated": len(results),
            "failed": len(errors),
        },
    }
    if meta:
        mode_label = {
            "none": "未做行业/市值中性化",
            "industry": "行业中性化（横截面剔除行业均值）",
            "size": "市值中性化（对 log 自由流通市值回归取残差）",
            "both": "行业 + 市值中性化（回归取残差）",
        }.get(cfg.neutralize, cfg.neutralize)
        payload["appendix"] = {
            "summary": {
                "data_range": meta.get("data_range"),
                "ret_period": f"{cfg.ret_period} 个交易日",
                "rebalance": f"每 {cfg.ret_period} 个交易日",
                "adjust": meta.get("adjust", "qfq（源侧预复权）"),
                "neutralize": mode_label,
                "cost": f"{cfg.cost_bps} bps（分层收益不扣费）",
            },
            "hygiene": {
                "exclude_suspended": cfg.exclude_suspended,
                "exclude_limit": cfg.exclude_limit,
                "limit_threshold": f"{cfg.limit_pct:.0%}（容差 {cfg.limit_tol:.1%}）",
                "st": "未覆盖（is_st 为死列）",
                "sub_new": f"以 {cfg.min_history} 行历史近似",
            },
            "coverage": {
                "industry": meta.get("industry_coverage"),
                "size_proxy": meta.get("weight_coverage"),
            },
            "caveats": [
                "IC 的 t 值按 iid 假设计算，日频 IC 存在自相关，显著性偏乐观",
                "分层为等权、不扣交易成本；真实收益需再扣换手成本",
                "成分股为当前快照（index_constituent.as_of_date），存在幸存者偏差",
                "市值代理用 csindex 成分权重（自由流通市值加权、含权重上限），"
                "非逐日市值；行业分类覆盖 "
                f"{meta.get('industry_coverage', '—')}，未覆盖记入「未知」组",
            ],
        }
    return payload

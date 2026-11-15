"""复权因子：按需从行情源反推并落库（K 线复权口径修复）。

## 为什么要有这个模块

2026-11-12 审计发现两件事：

1. ``stock_adjust_factor`` 基本是空壳——全库只有 5 只票有行，600519 的
   fore/back 因子**全是 1.0**。``/api/chart/kline?adjust=qfq`` 因此等于不复权：
   图上在除权日会留一个假跳空，和东财/同花顺默认的前复权图对不上。
2. 本地日线的**口径本身是混的**：``hithink_dump`` 的 daily-k 是不复权，而
   hithink 历史 K / akshare / 腾讯是前复权。实测 600519 存的是原始价
   （与腾讯 none 逐日一致），平安银行 000001 存的却已经是前复权价
   （与腾讯 none 差 2%+）。

## 做法

按需（打开某只票的 K 线时，且因子表缺/过期）取腾讯的 qfq 与 none 两条序列：

    factor_t = qfq_close_t / none_close_t

再用最近 60 个交易日判定本地属于哪种口径：

- 与 raw 更接近 → 本地是原始价，写**真实因子**，后续
  ``raw × factor_t / factor_last`` 就是前复权；
- 与 qfq 更接近 → 本地已是前复权，写 **1.0**（幂等，绝不能二次下调）。

这样不用改 DDL，也不用判断每只票当初是谁灌的——口径从数据自身比出来。
指数/板块没有复权概念，不处理；网络失败一律静默降级（继续用库里旧因子）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT = 8
_BARS = 1000                 # 腾讯单次上限约 1000 根（≈4 年），够 2 年图与回测窗口
_BASIS_WINDOW = 60           # 判定口径用的最近交易日数
_COVERAGE_MIN_ROWS = 200     # 因子行数达到它才认为"有覆盖"，否则重拉
_STALE_DAYS = 10             # 因子最新日期落后这么多天就重拉（停牌/长假留余量）
_DEV_TOLERANCE = 0.003       # 0.3%：判定"与某条序列一致"的容差
_ATTEMPT_TTL_S = 6 * 3600    # 同一只票反推失败后 6 小时内不再重试（网络/限流兜底）

# 进程内：最后一次反推时间，避免源被限流时每次打开图表都打两枪
_last_attempt: dict[str, float] = {}


def _bare(code: str) -> str:
    c = str(code).strip().lower()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            return c[2:]
    return c


def _symbol(code: str) -> str:
    """裸 6 位代码 → 腾讯需要的市场前缀。"""
    c = _bare(code)
    if c.startswith(("60", "68", "90", "50", "51", "52", "56", "58", "11")):
        return f"sh{c}"
    if c.startswith(("43", "83", "87", "88", "92")):
        return f"bj{c}"
    return f"sz{c}"


def _fetch(symbol: str, fq: str) -> pd.DataFrame:
    """取腾讯日线。fq: qfq（前复权）/ none（不复权）。失败抛异常，由上层兜。"""
    import requests

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    resp = requests.get(
        _BASE,
        params={"param": f"{symbol},day,{start},{end},{_BARS},{fq}"},
        headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    node = ((payload.get("data") or {}).get(symbol) or {})
    # 前复权键是 qfqday；不复权是 day
    rows = (node.get("qfqday") if fq == "qfq" else node.get("day")) or node.get("day") or []
    if not rows:
        return pd.DataFrame()
    out = []
    for item in rows:
        if len(item) < 5:
            continue
        out.append({
            "trade_date": str(item[0]),
            "open": float(item[1]),
            "close": float(item[2]),
            "high": float(item[3]),
            "low": float(item[4]),
        })
    return pd.DataFrame(out)


def derive_factors(code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """腾讯 qfq / none 两条序列 → 因子表（date, factor）与原始序列。"""
    sym = _symbol(code)
    raw = _fetch(sym, "none")
    qfq = _fetch(sym, "qfq")
    if raw.empty or qfq.empty:
        return pd.DataFrame(), raw
    merged = raw.merge(qfq, on="trade_date", suffixes=("_raw", "_qfq"))
    merged = merged[(merged["close_raw"] > 0) & (merged["close_qfq"] > 0)]
    if merged.empty:
        return pd.DataFrame(), raw
    factors = pd.DataFrame({
        "trade_date": merged["trade_date"],
        "fore_adjust_factor": (merged["close_qfq"] / merged["close_raw"]).round(6),
    })
    return factors, raw


def _deviation(stored: pd.DataFrame, ref: pd.DataFrame) -> Optional[float]:
    """本地收盘与参考序列的中位相对偏差（只在共同日期上算）。"""
    if stored is None or stored.empty or ref is None or ref.empty:
        return None
    a = stored[["trade_date", "close"]].dropna()
    a["trade_date"] = a["trade_date"].astype(str)
    m = a.merge(ref[["trade_date", "close"]], on="trade_date", suffixes=("_l", "_r"))
    if m.empty:
        return None
    m = m[m["close_r"] > 0]
    if m.empty:
        return None
    rel = (m["close_l"] - m["close_r"]).abs() / m["close_r"]
    return float(rel.median())


def detect_basis(stored: pd.DataFrame, raw: pd.DataFrame, qfq: pd.DataFrame) -> str:
    """判定本地序列是 'raw'（原始价）还是 'qfq'（已是前复权）。"""
    d_raw = _deviation(stored, raw)
    d_qfq = _deviation(stored, qfq)
    if d_raw is None and d_qfq is None:
        return "raw"           # 无从比较：按原始价处理（乘因子等价于用源侧 qfq）
    if d_qfq is None:
        return "raw"
    if d_raw is None:
        return "qfq"
    # 两条序列通常只差一点点（分红 1~3%），容差内按原始价处理更安全：
    # 若本地确实已是 qfq，写 1.0 也不会改变图形；写真实因子反而会二次下调。
    if d_qfq + _DEV_TOLERANCE < d_raw:
        return "qfq"
    return "raw"


def _existing_coverage(code: str) -> tuple[int, str, str] | None:
    from utils.cache.market_data_db import get_market_data_db

    with get_market_data_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(trade_date) lo, MAX(trade_date) hi, "
            "MIN(fore_adjust_factor) fmin, MAX(fore_adjust_factor) fmax "
            "FROM stock_adjust_factor WHERE code = ?", (_bare(code),),
        ).fetchone()
    if not row or not row["n"]:
        return None
    return (int(row["n"]), str(row["lo"]), str(row["hi"]))


def _stored_basis(code: str) -> str:
    """已落库因子是真实因子还是 1.0 哨兵。"""
    from utils.cache.market_data_db import get_market_data_db

    with get_market_data_db() as conn:
        row = conn.execute(
            "SELECT MIN(fore_adjust_factor) fmin, MAX(fore_adjust_factor) fmax, COUNT(*) n "
            "FROM stock_adjust_factor WHERE code = ?", (_bare(code),),
        ).fetchone()
    if not row or not row["n"]:
        return "raw"
    fmin, fmax = row["fmin"], row["fmax"]
    if fmin is None or fmax is None:
        return "raw"
    # 全为 1.0 = 本地已是前复权（或区间内没有分红），乘不乘都一样
    return "qfq" if abs(float(fmax) - 1.0) < 1e-6 and abs(float(fmin) - 1.0) < 1e-6 else "raw"


def _is_covered(code: str, stored: pd.DataFrame) -> bool:
    """因子是否够用。

    只看「行数够 + 尾部够新」，**不要求覆盖本地最早日期**：腾讯单次最多回 ~700 根
    （≈2.8 年），而图最长能拉 1000 根。更早的日期用最早一个因子 bfill（等价于
    "假设更早没有分红"）——整段等比缩放，不会在中途留一道缝；硬要求全覆盖的话，
    长窗口每次打开都会重拉网络。
    """
    cov = _existing_coverage(code)
    if not cov:
        return False
    n, _lo, hi = cov
    if n < _COVERAGE_MIN_ROWS:
        return False
    if stored is None or stored.empty:
        return True
    local_hi = str(stored["trade_date"].astype(str).max())
    try:
        lag = (datetime.strptime(local_hi, "%Y-%m-%d") - datetime.strptime(hi, "%Y-%m-%d")).days
    except ValueError:
        return False
    return lag <= _STALE_DAYS


def apply_factor_series(df: pd.DataFrame, factor_df: pd.DataFrame,
                        cols: tuple[str, ...] = ("open", "high", "low", "close", "pre_close"),
                        ) -> pd.DataFrame:
    """把 (trade_date, factor) 并入 OHLC，并按最新一根归一到 1（即前复权）。

    缺日期的因子前后向填充：左连接只命中部分日期时，**同一张图会一半复权、
    一半不复权**，均线中途断成两段。早于因子覆盖期的行用最早一个因子 bfill，
    等价于"假设更早没有分红"——整段等比缩放，不会留缝。
    """
    if df is None or df.empty or factor_df is None or factor_df.empty:
        return df
    merged = df.merge(factor_df, on="trade_date", how="left")
    if "factor" not in merged.columns or merged["factor"].notna().sum() == 0:
        return df
    merged["factor"] = merged["factor"].ffill().bfill()
    if merged["factor"].isna().any():
        return df
    latest = merged["factor"].iloc[-1]
    if not latest or latest <= 0:
        return df
    for col in cols:
        if col in merged.columns:
            merged[col] = merged[col] * merged["factor"] / latest
    return merged.drop(columns=["factor"])


def _store(code: str, factors: pd.DataFrame) -> int:
    from utils.cache.market_data_db import get_market_data_db

    if factors is None or factors.empty:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (_bare(code), str(r.trade_date), float(r.fore_adjust_factor), None, "derived", now)
        for r in factors.itertuples(index=False)
    ]
    with get_market_data_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_adjust_factor "
            "(code, trade_date, fore_adjust_factor, back_adjust_factor, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows,
        )
        conn.commit()
    return len(rows)


def ensure_factors(code: str, stored: pd.DataFrame | None = None,
                   *, force: bool = False) -> Optional[str]:
    """确保该 code 有可用的前复权因子；返回判定出的本地口径（raw/qfq）。

    - 已有够新够全的因子 → 直接返回（不再走网络）；
    - 否则取腾讯 qfq/none 反推并 upsert；
    - 任何失败都静默返回 None（调用方继续用库里旧因子，不阻断看图）。
    """
    code_bare = _bare(code)
    if not code_bare.isdigit() or len(code_bare) != 6:
        return None
    if not force and _is_covered(code_bare, stored):
        return _stored_basis(code_bare)
    if not force:
        import time as _time
        if (_time.time() - _last_attempt.get(code_bare, 0.0)) < _ATTEMPT_TTL_S:
            # 刚试过（可能失败了）：别再打网络，有多少因子用多少
            return _stored_basis(code_bare) if _existing_coverage(code_bare) else None
        _last_attempt[code_bare] = _time.time()
    try:
        factors, raw = derive_factors(code_bare)
        if factors.empty:
            return None
        qfq = raw.merge(factors, on="trade_date", how="inner")
        qfq["close"] = qfq["close"] * qfq["fore_adjust_factor"]
        basis = detect_basis(stored if stored is not None else pd.DataFrame(), raw, qfq)
        if basis == "qfq":
            # 本地已是前复权：写 1.0 哨兵，_apply_adjust 变成恒等变换
            factors = factors.assign(fore_adjust_factor=1.0)
        n = _store(code_bare, factors)
        logger.info("[adjust] %s 因子落库 %s 行，本地口径=%s", code_bare, n, basis)
        return basis
    except Exception as e:  # noqa: BLE001 - 复权失败不该让图打不开
        logger.debug("[adjust] %s 因子反推失败（继续用库里旧因子）: %s", code_bare, e)
        return None

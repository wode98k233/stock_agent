"""因子评估的数据接入层（唯一允许碰 DB 的地方）。

纯计算在 ``factor_lib`` / ``factor_eval``，本模块只负责「把库里的行变成
DataFrame」。分层清晰才能在单测里用合成面板跑通而不碰真实库。

口径要点
--------
- 库内 ``stock_daily`` 已是**源侧预复权**（P0 实证，见
  docs/superpowers/specs/2026-10-15-backtest-p0-matching-fidelity-design.md §11.8.3）
  → 本层**不得再做复权**，否则二重下调
- 标的池取 ``index_constituent`` 的最新快照（``as_of_date`` 最大值）；
  只反映当下成分，存在幸存者偏差——报告附录必须声明
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence

import pandas as pd

from utils.cache.market_data_db import get_market_data_db

# 因子预热所需的历史交易日（MA60 / DMI / CCI 等最长窗口 ~60）
DEFAULT_WARMUP_BARS = 90

_COLUMNS = ("code", "trade_date", "open", "high", "low", "close",
            "pct_change", "volume", "amount")


def load_universe(index_code: str = "000300",
                  as_of: Optional[str] = None) -> List[dict]:
    """读取指数成分快照（含权重与行业，供中性化使用）。

    Returns:
        ``[{code, name, as_of_date, exchange, in_date, weight, industry}, ...]``
    """
    with get_market_data_db() as conn:
        if as_of:
            date = as_of
        else:
            row = conn.execute(
                "SELECT MAX(as_of_date) FROM index_constituent WHERE index_code = ?",
                (index_code,)).fetchone()
            date = row[0] if row else None
        if not date:
            return []
        rows = conn.execute(
            "SELECT c.code, c.name, c.as_of_date, c.exchange, c.in_date, c.weight, "
            "       i.industry "
            "FROM index_constituent c "
            "LEFT JOIN stock_info i ON i.code = c.code "
            "WHERE c.index_code = ? AND c.as_of_date = ? "
            "ORDER BY c.code", (index_code, date)).fetchall()
    return [dict(r) for r in rows]


def trading_dates_before(start_date: str, bars: int) -> str:
    """取 ``start_date`` 之前第 ``bars`` 个交易日（用于预热窗口起点）。"""
    if bars <= 0:
        return start_date
    with get_market_data_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily "
            "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?",
            (start_date, bars)).fetchall()
    if len(rows) < bars:
        return rows[-1][0] if rows else start_date
    return rows[-1][0]


def load_frames(codes: Sequence[str], start_date: str, end_date: str,
                warmup_bars: int = DEFAULT_WARMUP_BARS) -> Dict[str, pd.DataFrame]:
    """批量读取多票日线（含预热窗口）。

    ⚠️ 预热必须在这里做：因子在 ``start_date`` 当天就需要有值，若从 start 才开始
    加载，MA60 类因子前 60 行全是 NaN，评估区间会被凭空削掉两个月。
    """
    if not codes:
        return {}
    warm_start = trading_dates_before(start_date, warmup_bars)
    placeholders = ",".join("?" * len(codes))
    sql = f"""SELECT {', '.join(_COLUMNS)} FROM stock_daily
              WHERE code IN ({placeholders})
                AND trade_date >= ? AND trade_date <= ?
              ORDER BY code, trade_date"""

    with get_market_data_db() as conn:
        df = pd.read_sql_query(sql, conn, params=[*codes, warm_start, end_date])

    if df.empty:
        return {}

    df["trade_date"] = df["trade_date"].astype(str)
    out: Dict[str, pd.DataFrame] = {}
    for code, g in df.groupby("code", sort=False):
        out[str(code)] = g.reset_index(drop=True)
    return out


def load_panel_inputs(index_code: str = "000300",
                      start_date: str = "2024-01-01",
                      end_date: Optional[str] = None,
                      warmup_bars: int = DEFAULT_WARMUP_BARS,
                      codes: Optional[Sequence[str]] = None) -> tuple[Dict[str, pd.DataFrame], dict]:
    """读取「标的池 + 日线」，返回 ``(frames, meta)``。

    ``meta["meta_map"]`` 是 ``{code: {industry, size}}``，供中性化使用：
    - ``industry``：stock_info.industry（akshare/sina 行业，去重后最具体分类）
    - ``size``：**成分权重**作自由流通市值代理（见 enrich_universe_meta 的说明）
    """
    end = end_date or datetime.now().strftime("%Y-%m-%d")
    universe = load_universe(index_code)
    pool = list(codes) if codes else [r["code"] for r in universe]
    names = {r["code"]: r.get("name") or "" for r in universe}
    meta_map = {
        r["code"]: {"industry": r.get("industry"), "size": r.get("weight")}
        for r in universe
    }

    frames = load_frames(pool, start_date, end, warmup_bars)
    with_industry = sum(1 for r in universe if r.get("industry"))
    with_weight = sum(1 for r in universe if r.get("weight") is not None)
    meta = {
        "index_code": index_code,
        "as_of_date": universe[0]["as_of_date"] if universe else None,
        "universe_size": len(pool),
        "loaded": len(frames),
        "missing": sorted(set(pool) - set(frames)),
        "start": start_date,
        "end": end,
        "warmup_bars": warmup_bars,
        "adjust": "qfq（源侧预复权，评估层不复权）",
        "names": names,
        "meta_map": meta_map,
        "industry_coverage": f"{with_industry}/{len(pool)}" if pool else "0/0",
        "weight_coverage": f"{with_weight}/{len(pool)}" if pool else "0/0",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if frames:
        all_dates = [f["trade_date"].max() for f in frames.values()]
        meta["data_range"] = f"{start_date} ~ {max(all_dates)}"
    else:
        meta["data_range"] = None
    return frames, meta

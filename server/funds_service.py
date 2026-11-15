"""个股资金面：融资融券余额 + 龙虎榜营业部席位。

- 融资融券：读本地 `margin_daily`（由 `scripts/ops/sync_margin_data.py` 同步），
  不再依赖已停更的北向个股接口。
- 龙虎榜：akshare 东财按需取数 + 5 分钟内存缓存。

不接入 LLM 工具面；只服务 Web API（K 线弹窗「资金面」条）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 300
_cache: Dict[tuple, tuple] = {}

LHB_LOOKBACK_DAYS = 180
MARGIN_DAYS = 60


def _cached(key: tuple, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _d(value) -> Optional[str]:
    try:
        ts = pd.Timestamp(value)
    except Exception:  # noqa: BLE001
        return None
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _num(value) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 融资融券
# ---------------------------------------------------------------------------

def _pct_change(rows: List[Dict[str, Any]], n: int) -> Optional[float]:
    if len(rows) <= n:
        return None
    now, base = rows[-1].get("fin_balance"), rows[-1 - n].get("fin_balance")
    if now is None or not base:
        return None
    return round((now - base) / base * 100, 2)


def summarize_margin(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """融资融券日序列（日期升序）→ 最新值、环比/5日/20日变化、近 N 日分位与风险档。"""
    rows = [r for r in rows if r.get("trade_date")]
    if not rows:
        return {"available": False, "latest": None, "metrics": {},
                "series": [], "note": "本地无该标的融资融券数据（先跑 sync_margin_data.py）"}
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    fin = latest.get("fin_balance")
    chg = None
    chg_pct = None
    if prev and prev.get("fin_balance") and fin is not None:
        chg = fin - prev["fin_balance"]
        chg_pct = round(chg / prev["fin_balance"] * 100, 2)
    balances = [r["fin_balance"] for r in rows if r.get("fin_balance") is not None]
    rank = None
    if balances and fin is not None:
        rank = round(sum(1 for b in balances if b <= fin) / len(balances), 2)
    if rank is None:
        risk, risk_note = "unknown", ""
    elif rank >= 0.8:
        risk, risk_note = "high", f"融资余额处于近 {len(balances)} 日高位（分位 {rank:.0%}），注意去杠杆风险"
    elif rank >= 0.5:
        risk, risk_note = "mid", f"融资余额处于近 {len(balances)} 日中位偏上（分位 {rank:.0%}）"
    else:
        risk, risk_note = "low", f"融资余额处于近 {len(balances)} 日低位（分位 {rank:.0%}）"
    return {
        "available": True,
        "latest": latest,
        "metrics": {
            "fin_balance": fin,
            "fin_buy": latest.get("fin_buy"),
            "sec_balance": latest.get("sec_balance"),
            "total_balance": latest.get("total_balance"),
            "change": chg,
            "change_pct": chg_pct,
            "change_5d_pct": _pct_change(rows, 5),
            "change_20d_pct": _pct_change(rows, 20),
            "percentile": rank,
        },
        "risk": risk,
        "risk_note": risk_note,
        "series": [{"date": r["trade_date"], "fin_balance": r.get("fin_balance")}
                   for r in rows if r.get("fin_balance") is not None],
        "note": "",
    }


def get_margin(code: str, days: int = MARGIN_DAYS) -> Dict[str, Any]:
    from utils.cache.market_data_db import query_margin_daily
    try:
        return summarize_margin(query_margin_daily(code, days))
    except Exception as e:  # noqa: BLE001
        logger.warning("融资融券读取失败 %s: %s", code, e)
        return {"available": False, "latest": None, "metrics": {}, "series": [],
                "note": f"读取失败：{e}"}


# ---------------------------------------------------------------------------
# 龙虎榜
# ---------------------------------------------------------------------------

def normalize_lhb_seats(df: pd.DataFrame, flag: str) -> List[Dict[str, Any]]:
    """龙虎榜席位宽表 → 统一记录（金额为元，占比为百分数）。"""
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        buy_ratio = _num(r.get("买入金额-占总成交比例"))
        sell_ratio = _num(r.get("卖出金额-占总成交比例"))
        out.append({
            "seat": str(r.get("交易营业部名称") or "").strip(),
            "buy": _num(r.get("买入金额")),
            "sell": _num(r.get("卖出金额")),
            "net": _num(r.get("净额")),
            "buy_pct": round(buy_ratio * 100, 2) if buy_ratio is not None else None,
            "sell_pct": round(sell_ratio * 100, 2) if sell_ratio is not None else None,
            "reason": str(r.get("类型") or "").strip(),
            "side": flag,
        })
    return out


def pick_latest_lhb_date(dates: List[str], today: Optional[date] = None,
                         lookback_days: int = LHB_LOOKBACK_DAYS) -> Optional[str]:
    """从历史日期里挑最近一次（lookback 内）；没有则 None。"""
    today = today or date.today()
    floor = (today - timedelta(days=max(1, int(lookback_days)))).isoformat()
    valid = [d for d in dates if d and floor <= d <= today.isoformat()]
    return max(valid) if valid else None


def get_lhb(code: str, date_str: Optional[str] = None) -> Dict[str, Any]:
    """最近一次上榜日期 + 买卖席位明细；窗口内未上榜返回 available=False。"""
    def _fetch():
        import akshare as ak
        try:
            if date_str:
                picked = date_str
            else:
                df_dates = ak.stock_lhb_stock_detail_date_em(symbol=code)
                col = df_dates.get("交易日") if df_dates is not None else None
                dates = [_d(x) for x in (col.tolist() if col is not None else [])]
                picked = pick_latest_lhb_date([x for x in dates if x])
            if not picked:
                return {"available": False, "date": None, "seats": [],
                        "note": f"近 {LHB_LOOKBACK_DAYS} 天未上龙虎榜"}
            ymd = picked.replace("-", "")
            buy = normalize_lhb_seats(ak.stock_lhb_stock_detail_em(
                symbol=code, date=ymd, flag="买入"), "buy")
            sell = normalize_lhb_seats(ak.stock_lhb_stock_detail_em(
                symbol=code, date=ymd, flag="卖出"), "sell")
            return {"available": bool(buy or sell), "date": picked,
                    "seats": buy + sell, "note": ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("龙虎榜席位取数失败 %s: %s", code, e)
            return {"available": False, "date": None, "seats": [], "note": f"取数失败：{e}"}
    return _cached(("lhb", code, date_str or ""), _fetch)


def get_stock_funds(code: str) -> Dict[str, Any]:
    return {"code": code, "margin": get_margin(code), "lhb": get_lhb(code)}

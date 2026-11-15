#!/usr/bin/env python
"""融资融券日度同步：沪深两市个股融资余额/买入额/融券余额 → margin_daily。

设计：docs/arch/2026-11-07-资金面-融资融券设计.md（随 K 线「资金面」条落地）
不做常驻 job；手动或系统计划任务跑。每日 2 个请求（沪/深各一次全市场明细）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _num(value) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def parse_sse(df: pd.DataFrame, trade_date: str) -> List[Dict[str, Any]]:
    """上交所明细 → margin_daily 记录（无融券余额，total_balance 留空）。"""
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        code = str(r.get("标的证券代码") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "code": code, "trade_date": trade_date, "market": "sh",
            "fin_balance": _num(r.get("融资余额")),
            "fin_buy": _num(r.get("融资买入额")),
            "fin_repay": _num(r.get("融资偿还额")),
            "sec_balance": None,
            "total_balance": None,
            "source": "akshare:sse",
        })
    return out


def parse_szse(df: pd.DataFrame, trade_date: str) -> List[Dict[str, Any]]:
    """深交所明细 → margin_daily 记录（无融资偿还额，fin_repay 留空）。"""
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        code = str(r.get("证券代码") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "code": code, "trade_date": trade_date, "market": "sz",
            "fin_balance": _num(r.get("融资余额")),
            "fin_buy": _num(r.get("融资买入额")),
            "fin_repay": None,
            "sec_balance": _num(r.get("融券余额")),
            "total_balance": _num(r.get("融资融券余额")),
            "source": "akshare:szse",
        })
    return out


def iter_trade_dates(days: int, today: Optional[date] = None) -> List[str]:
    """近 days 个自然日里属于交易日的日期（升序）。"""
    from utils.trading_calendar import is_market_open

    today = today or date.today()
    out = []
    for i in range(max(1, int(days)) - 1, -1, -1):
        d = today - timedelta(days=i)
        if is_market_open("cn", d):
            out.append(d.isoformat())
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="融资融券日度同步（沪/深个股明细）")
    p.add_argument("--days", type=int, default=60, help="回溯自然日（默认 60）")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不联网/不写库")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    dates = iter_trade_dates(args.days)
    print(f"[MARGIN] 交易日 {len(dates)} 天：{dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'}")
    if args.dry_run:
        return 0

    import akshare as ak
    from utils.cache.market_data_db import upsert_margin_daily

    total = 0
    failed: List[str] = []
    for d in dates:
        ymd = d.replace("-", "")
        try:
            total += upsert_margin_daily(parse_sse(ak.stock_margin_detail_sse(date=ymd), d))
        except Exception as e:  # noqa: BLE001
            failed.append(f"sse {d}: {e}")
        try:
            total += upsert_margin_daily(parse_szse(ak.stock_margin_detail_szse(date=ymd), d))
        except Exception as e:  # noqa: BLE001
            failed.append(f"szse {d}: {e}")
        if failed and len(failed) > 20:
            break

    print(f"[MARGIN] 入库 {total} 条，失败 {len(failed)} 次")
    for f in failed[:10]:
        print(f"[MARGIN] 失败: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

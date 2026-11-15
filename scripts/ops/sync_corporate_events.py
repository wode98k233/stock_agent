#!/usr/bin/env python
"""公司事件同步：解禁 / 分红除权 / 预约披露 / 股东大会 / 业绩预告 → corporate_events。

设计：docs/arch/2026-11-07-公司事件日历-设计.md
不做常驻 job；手动或系统计划任务跑。默认窗口：过去 30 天 ~ 未来 60 天。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STATUS = ROOT / "output" / "sync_events_status.json"


# ---------------------------------------------------------------------------
# 纯函数（单测覆盖，不联网）
# ---------------------------------------------------------------------------

def event_id_for(code: str, event_type: str, event_date: str, key: str) -> str:
    raw = f"{code}|{event_type}|{event_date}|{key}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _d(value) -> Optional[str]:
    """任意日期值 → YYYY-MM-DD；NaT/None/非法 → None。"""
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
        ts = pd.Timestamp(value)
    except Exception:  # noqa: BLE001
        return None
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _in_window(d: Optional[str], start: str, end: str) -> bool:
    return bool(d) and start <= d <= end


def _rec(code, etype, edate, title, payload, source="akshare") -> Dict[str, Any]:
    return {"event_id": event_id_for(str(code), etype, edate, title),
            "code": str(code), "event_type": etype, "event_date": edate,
            "title": title, "payload": payload, "source": source}


def parse_unlock(df, start: str, end: str) -> List[Dict[str, Any]]:
    out = []
    for _, r in df.iterrows():
        d = _d(r.get("解禁时间"))
        if not _in_window(d, start, end):
            continue
        code, name = str(r.get("股票代码")), str(r.get("股票简称"))
        out.append(_rec(code, "unlock", d,
                        f"{name} · {r.get('限售股类型') or '解禁'}",
                        {"qty": r.get("解禁数量"), "mv": r.get("实际解禁市值"),
                         "ratio": r.get("占解禁前流通市值比例")}, "akshare:em"))
    return out


def parse_dividend(df, start: str, end: str) -> List[Dict[str, Any]]:
    out = []
    for _, r in df.iterrows():
        d = _d(r.get("除权除息日"))
        if not _in_window(d, start, end):
            continue
        code, name = str(r.get("代码")), str(r.get("名称"))
        out.append(_rec(code, "dividend", d, f"{name} · 除权除息",
                        {"cash_per_10": r.get("现金分红-现金分红比例"),
                         "record_date": _d(r.get("股权登记日")),
                         "progress": r.get("方案进度")}, "akshare:em"))
    return out


def parse_disclosure(df, start: str, end: str, period: str = "") -> List[Dict[str, Any]]:
    out = []
    for _, r in df.iterrows():
        if _d(r.get("实际披露")):
            continue
        d = None
        # 以「最新一次预约/变更」为准：三次 > 二次 > 初次 > 首次
        for col in ("三次变更", "二次变更", "初次变更", "首次预约"):
            if d is None:
                d = _d(r.get(col))
        if not _in_window(d, start, end):
            continue
        code, name = str(r.get("股票代码")), str(r.get("股票简称"))
        out.append(_rec(code, "disclosure", d, f"{name} · {period} 预约披露",
                        {"first": _d(r.get("首次预约")), "period": period},
                        "akshare:cninfo"))
    return out


def parse_meeting(df, start: str, end: str) -> List[Dict[str, Any]]:
    out = []
    for _, r in df.iterrows():
        d = _d(r.get("召开开始日"))
        if not _in_window(d, start, end):
            continue
        code, name = str(r.get("代码")), str(r.get("简称"))
        out.append(_rec(code, "meeting", d, f"{name} · {r.get('股东大会名称')}",
                        {"record_date": _d(r.get("股权登记日")),
                         "proposal": str(r.get("提案") or "")[:200]}, "akshare:em"))
    return out


def parse_forecast(df, start: str, end: str) -> List[Dict[str, Any]]:
    out = []
    for _, r in df.iterrows():
        d = _d(r.get("公告日期"))
        if not _in_window(d, start, end):
            continue
        code, name = str(r.get("股票代码")), str(r.get("股票简称"))
        out.append(_rec(code, "forecast", d, f"{name} · 业绩预告（{r.get('预告类型')}）",
                        {"change": r.get("业绩变动"), "pct": r.get("业绩变动幅度")},
                        "akshare:em"))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="公司事件同步（解禁/分红/披露/股东大会/预告）")
    p.add_argument("--days-back", type=int, default=30)
    p.add_argument("--days-ahead", type=int, default=60)
    p.add_argument("--types", default="unlock,dividend,disclosure,meeting,forecast")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--status-file", default=str(DEFAULT_STATUS))
    return p


def collect(start: str, end: str, types: List[str]) -> tuple[list, list]:
    """按类型拉取并解析；单类失败不阻断。返回 (records, failures)。"""
    import akshare as ak

    records: List[Dict[str, Any]] = []
    failed: List[str] = []
    if "unlock" in types:
        try:
            records += parse_unlock(ak.stock_restricted_release_detail_em(
                start_date=start.replace("-", ""), end_date=end.replace("-", "")), start, end)
        except Exception as e:  # noqa: BLE001
            failed.append(f"unlock: {e}")
    if "dividend" in types:
        for period in ("20260630", "20251231"):
            try:
                records += parse_dividend(ak.stock_fhps_em(date=period), start, end)
            except Exception as e:  # noqa: BLE001
                failed.append(f"dividend {period}: {e}")
    if "disclosure" in types:
        # 11 月只有三季报；2026 年报预约要到 12 月/次年 1 月才发布，
        # 届时把 ("2026三季",) 扩成 ("2026三季", "2026年报")。
        for period in ("2026三季",):
            try:
                records += parse_disclosure(
                    ak.stock_report_disclosure(market="沪深京", period=period),
                    start, end, period)
            except Exception as e:  # noqa: BLE001
                failed.append(f"disclosure {period}: {e}")
    if "meeting" in types:
        try:
            records += parse_meeting(ak.stock_gddh_em(), start, end)
        except Exception as e:  # noqa: BLE001
            failed.append(f"meeting: {e}")
    if "forecast" in types:
        for period in ("20260930", "20261231"):
            try:
                records += parse_forecast(ak.stock_yjyg_em(date=period), start, end)
            except Exception as e:  # noqa: BLE001
                failed.append(f"forecast {period}: {e}")
    return records, failed


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    start = (date.today() - timedelta(days=args.days_back)).isoformat()
    end = (date.today() + timedelta(days=args.days_ahead)).isoformat()
    types = [t.strip() for t in args.types.split(",") if t.strip()]

    records, failed = collect(start, end, types)
    print(f"[EVENTS] 窗口 {start} ~ {end}，解析 {len(records)} 条，失败 {len(failed)} 类")
    for f in failed:
        print(f"[EVENTS] 失败: {f}")
    if args.dry_run:
        return 0

    from utils.cache.market_data_db import upsert_corporate_events
    n = upsert_corporate_events(records)
    print(f"[EVENTS] 入库 {n} 条")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

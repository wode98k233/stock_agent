"""公司事件查询服务：日历图层、持仓/自选事件条、技能工具共用。

数据由 `scripts/ops/sync_corporate_events.py` 写入 `market_data.db.corporate_events`。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from utils.cache.market_data_db import query_corporate_events

EVENT_LABELS = {
    "unlock": "解禁",
    "dividend": "分红除权",
    "disclosure": "预约披露",
    "meeting": "股东大会",
    "forecast": "业绩预告",
}


def list_events(*, codes: Optional[List[str]] = None, date_from: str = "",
                date_to: str = "", types: Optional[List[str]] = None,
                limit: int = 500) -> List[Dict[str, Any]]:
    """查询事件并补上展示用 type_label；日期升序。"""
    rows = query_corporate_events(codes=codes, date_from=date_from or None,
                                  date_to=date_to or None, types=types,
                                  limit=limit)
    for r in rows:
        r["type_label"] = EVENT_LABELS.get(r["event_type"], r["event_type"])
    return rows


def group_by_date(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["event_date"], []).append(r)
    return out


def load_corporate_events(year: int, month: int,
                          limit: int = 500) -> Dict[str, List[Dict[str, Any]]]:
    """某月的事件，按日期分组（日历用）。"""
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-31"
    return group_by_date(list_events(date_from=start, date_to=end, limit=limit))

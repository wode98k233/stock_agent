"""Fast calendar queries for trace runs（分库后逐月库查询合并）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from utils.token_usage import normalize_token_usage_dict


def month_bounds(year: int | None, month: int | None) -> tuple[str | None, str | None]:
    if not year:
        return None, None
    if not month:
        return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"
    if month < 1 or month > 12:
        raise ValueError("month must be between 1 and 12")
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _yms_between(start: str, end: str) -> list[str]:
    """[start, end) 覆盖的月份键列表（YYYYMM，升序；end 为排他边界不含）。"""
    ym_start = (start or "")[:7].replace("-", "")
    ym_end = (end or "")[:7].replace("-", "")
    if not ym_start or not ym_end:
        return []
    out = []
    y, m = int(ym_start[:4]), int(ym_start[4:])
    while True:
        ym = f"{y:04d}{m:02d}"
        if ym == ym_end:  # end 排他
            break
        out.append(ym)
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if len(out) > 1200:  # 防御：异常入参兜底（100 年）
            break
    return out


def _month_where(ym: str) -> tuple[str, list[str]]:
    """单月库的时间过滤（created_at 落在该月）。"""
    start = f"{ym[:4]}-{ym[4:]}-01"
    y, m = int(ym[:4]), int(ym[4:])
    if m == 12:
        end = f"{y + 1:04d}-01-01"
    else:
        end = f"{y:04d}-{m + 1:02d}-01"
    return "WHERE created_at>=? AND created_at<?", [start, end]


def _acc_token_map(dst: dict, add: dict) -> None:
    for rid, s in add.items():
        cur = dst.setdefault(rid, {k: 0 for k in (
            "llm_calls", "input_tokens", "output_tokens",
            "total_tokens", "cached_tokens", "reasoning_tokens")})
        for k, v in s.items():
            cur[k] = cur.get(k, 0) + v


def query_trace_calendar(store, year: int | None, month: int | None) -> dict[str, Any]:
    """按日期分组返回 run 统计和 token 消耗（分库：逐月库查询后合并）。

    year/month 都缺省时遍历全部月库；指定时只打覆盖范围内的月库。
    """
    conns: list[tuple[str, Any]] = []
    if year:
        start, end = month_bounds(year, month)
        for ym in _yms_between(start, end):
            conn = store.conn_for_month(ym)
            if conn is not None:
                conns.append((ym, conn))
    else:
        for ym in store.iter_existing_months():
            conn = store.conn_for_month(ym)
            if conn is not None:
                conns.append((ym, conn))

    run_days: dict[str, list[dict[str, Any]]] = {}
    token_map: dict[str, dict[str, int]] = {}
    for ym, conn in conns:
        where, params = _month_where(ym)
        rows = conn.execute(
            f"""
            SELECT id, agent_name, status, created_at, duration_ms
            FROM runs
            {where}
            ORDER BY created_at DESC
            """,
            params,
        ).fetchall()
        for row in rows:
            run = dict(row)
            day_str = _day_from_created_at(run.get("created_at"))
            run_days.setdefault(day_str, []).append(run)
        _acc_token_map(token_map, _query_token_map(conn, *_month_bounds_of(ym)))

    for runs in run_days.values():
        for run in runs:
            stats = token_map.get(run["id"], {})
            run["llm_calls"] = stats.get("llm_calls", 0)
            run["input_tokens"] = stats.get("input_tokens", 0)
            run["output_tokens"] = stats.get("output_tokens", 0)
            run["total_tokens"] = stats.get("total_tokens", 0)
            run["cached_tokens"] = stats.get("cached_tokens", 0)
            run["reasoning_tokens"] = stats.get("reasoning_tokens", 0)

    return {"year": year, "month": month, "run_days": run_days}


def _month_bounds_of(ym: str) -> tuple[str | None, str | None]:
    return month_bounds(int(ym[:4]), int(ym[4:]))


def _query_token_map(conn: sqlite3.Connection, start: str | None, end: str | None) -> dict[str, dict[str, int]]:
    where = ""
    params: list[Any] = []
    if start and end:
        where = "AND r.created_at>=? AND r.created_at<?"
        params = [start, end]

    steps = conn.execute(
        f"""
        SELECT s.run_id, s.extra
        FROM runs r
        JOIN steps s ON s.run_id = r.id
        WHERE s.step_type='llm'
        {where}
        """,
        params,
    ).fetchall()

    token_map: dict[str, dict[str, int]] = {}
    for step in steps:
        run_id = step["run_id"]
        extra = step["extra"]
        if not extra:
            continue
        try:
            data = json.loads(extra)
            usage = normalize_token_usage_dict(data.get("token_usage"))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if run_id not in token_map:
            token_map[run_id] = {
                "llm_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
            }
        token_map[run_id]["llm_calls"] += 1
        token_map[run_id]["input_tokens"] += usage["input_tokens"]
        token_map[run_id]["output_tokens"] += usage["output_tokens"]
        token_map[run_id]["total_tokens"] += usage["total_tokens"]
        token_map[run_id]["cached_tokens"] += usage.get("cached_tokens", 0)
        token_map[run_id]["reasoning_tokens"] += usage.get("reasoning_tokens", 0)
    return token_map


def _day_from_created_at(created_at: str | None) -> str:
    created = created_at or ""
    try:
        return datetime.fromisoformat(created).strftime("%Y-%m-%d")
    except Exception:
        return created[:10]

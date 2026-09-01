"""Fast calendar queries for trace runs."""

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


def query_trace_calendar(conn: sqlite3.Connection, year: int | None, month: int | None) -> dict[str, Any]:
    start, end = month_bounds(year, month)

    where = ""
    params: list[Any] = []
    if start and end:
        where = "WHERE created_at>=? AND created_at<?"
        params = [start, end]

    rows = conn.execute(
        f"""
        SELECT id, agent_name, status, created_at, duration_ms
        FROM runs
        {where}
        ORDER BY created_at DESC
        """,
        params,
    ).fetchall()

    if not rows:
        return {"year": year, "month": month, "run_days": {}}

    run_days: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        run = dict(row)
        day_str = _day_from_created_at(run.get("created_at"))
        run_days.setdefault(day_str, []).append(run)

    token_map = _query_token_map(conn, start, end)
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

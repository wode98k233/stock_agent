"""日历服务：交易日、对话记录、token 统计。"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def local_month_utc_range(year: int, month: int) -> tuple[str, str]:
    """返回本地月份对应的 UTC 半开区间。"""
    tz = ZoneInfo("Asia/Shanghai")
    start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def load_trading_days(year: int, month: int) -> list[str]:
    """读取交易日历 JSON，返回指定月份的交易日列表。"""
    from utils.app_paths import get_data_path
    cal_path = Path(get_data_path()) / "trading_calendar.json"
    try:
        if cal_path.exists():
            with open(cal_path, encoding="utf-8") as f:
                cal_data = json.load(f)
            sessions = cal_data.get("markets", {}).get("cn", {}).get("sessions", [])
            prefix = f"{year}-{month:02d}-"
            return [s for s in sessions if s.startswith(prefix)]
    except Exception:
        logger.warning("日历: 读取 trading_calendar.json 失败")
    return []


def load_dialog_days(storage, year: int, month: int) -> dict[str, list]:
    """查询对话记录，按本地日期分组。"""
    dialog_days: dict[str, list] = {}
    try:
        start_at, end_at = local_month_utc_range(year, month)
        all_dialogs = storage.list_dialogs_by_created_range(start_at, end_at)
        month_prefix = f"{year}-{month:02d}-"
        for d in all_dialogs:
            created = d.get("created_at", "")
            if not created:
                continue
            try:
                dt = datetime.fromisoformat(created)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                local_date = dt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
            except Exception:
                continue
            if not local_date.startswith(month_prefix):
                continue
            dialog_days.setdefault(local_date, []).append({
                "dialog_uuid": d["dialog_uuid"],
                "title": d["title"],
                "mode": d.get("current_mode", ""),
                "created_at": created,
            })
    except Exception:
        logger.warning("日历: 查询对话记录失败")
    return dialog_days


def load_trace_token_stats(storage, dialog_days: dict[str, list]) -> dict[str, dict]:
    """查询每个对话的 token 统计（从 trace DB）。"""
    trace_token_map: dict[str, dict] = {}
    try:
        from utils.app_paths import get_trace_db_path
        from utils.token_usage import normalize_token_usage_dict
        trace_db = get_trace_db_path()
        if not Path(trace_db).exists():
            return trace_token_map

        all_uuids = []
        for dialogs in dialog_days.values():
            for dlg in dialogs:
                all_uuids.append(dlg["dialog_uuid"])

        if not all_uuids:
            return trace_token_map

        conn = sqlite3.connect(trace_db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            dialog_run_map = storage.latest_trace_runs_for_dialogs(all_uuids)
            run_ids = list(set(dialog_run_map.values()))
            if not run_ids:
                return trace_token_map

            ph2 = ",".join("?" * len(run_ids))
            run_rows = conn.execute(
                f"SELECT id, duration_ms FROM runs WHERE id IN ({ph2})",
                run_ids,
            ).fetchall()
            run_dur_map = {r["id"]: r["duration_ms"] for r in run_rows}

            step_rows = conn.execute(
                f"SELECT run_id, extra FROM steps "
                f"WHERE run_id IN ({ph2}) AND step_type='llm'",
                run_ids,
            ).fetchall()

            token_map: dict[str, dict] = {}
            for s in step_rows:
                rid = s["run_id"]
                extra_str = s["extra"]
                if not extra_str or not isinstance(extra_str, str):
                    continue
                try:
                    d = json.loads(extra_str)
                    tu = normalize_token_usage_dict(d.get("token_usage"))
                    if rid not in token_map:
                        token_map[rid] = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
                    token_map[rid]["llm_calls"] += 1
                    token_map[rid]["input_tokens"] += tu["input_tokens"]
                    token_map[rid]["output_tokens"] += tu["output_tokens"]
                    token_map[rid]["total_tokens"] += tu["total_tokens"]
                    token_map[rid]["cached_tokens"] += tu.get("cached_tokens", 0)
                    token_map[rid]["reasoning_tokens"] += tu.get("reasoning_tokens", 0)
                except Exception:
                    continue

            for dlg_uuid, run_id in dialog_run_map.items():
                tm = token_map.get(run_id, {})
                trace_token_map[dlg_uuid] = {
                    "llm_calls": tm.get("llm_calls", 0),
                    "input_tokens": tm.get("input_tokens", 0),
                    "output_tokens": tm.get("output_tokens", 0),
                    "total_tokens": tm.get("total_tokens", 0),
                    "cached_tokens": tm.get("cached_tokens", 0),
                    "reasoning_tokens": tm.get("reasoning_tokens", 0),
                    "duration_ms": run_dur_map.get(run_id),
                }
        finally:
            conn.close()
    except Exception:
        logger.warning("日历: 查询 trace token 统计失败", exc_info=True)
    return trace_token_map


def build_calendar_payload(storage, year: int, month: int) -> dict:
    """构建日历响应数据。"""
    trading_days = load_trading_days(year, month)
    dialog_days = load_dialog_days(storage, year, month)
    trace_token_map = load_trace_token_stats(storage, dialog_days)

    for dialogs in dialog_days.values():
        for dlg in dialogs:
            stats = trace_token_map.get(dlg["dialog_uuid"], {})
            dlg["llm_calls"] = stats.get("llm_calls", 0)
            dlg["input_tokens"] = stats.get("input_tokens", 0)
            dlg["output_tokens"] = stats.get("output_tokens", 0)
            dlg["total_tokens"] = stats.get("total_tokens", 0)
            dlg["cached_tokens"] = stats.get("cached_tokens", 0)
            dlg["reasoning_tokens"] = stats.get("reasoning_tokens", 0)
            dlg["duration_ms"] = stats.get("duration_ms")

    return {"year": year, "month": month, "trading_days": trading_days, "dialog_days": dialog_days}

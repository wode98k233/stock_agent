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


def load_deep_report_days(year: int, month: int, *, db=None) -> dict[str, list]:
    """查询长报告元数据，按本地日期分组（独立字段，**不混进 dialog_days**）。

    ⚠️ 时间口径与 web_dialogs 不同：deep_report.created_at 是**本地 naive**
    （agents/deep_report/store.py::_now = datetime.now()），不是 UTC ISO，
    因此不能套 local_month_utc_range；这里直接用本地月份字符串区间。
    """
    report_days: dict[str, list] = {}
    try:
        if db is None:
            from utils.app_paths import get_deep_report_db_path
            path = Path(get_deep_report_db_path())
            # 只读接口不建库：没有报告库时直接返回空
            if not path.exists():
                return report_days
            from agents.deep_report.db import get_db
            db = get_db()
        ctx = getattr(db, "_ctx", None)
        if ctx is None:
            return report_days

        prefix = f"{year:04d}-{month:02d}-"
        start = f"{prefix}01 00:00:00"
        if month == 12:
            end = f"{year + 1:04d}-01-01 00:00:00"
        else:
            end = f"{year:04d}-{month + 1:02d}-01 00:00:00"
        with ctx() as conn:
            rows = conn.execute(
                "SELECT rid,title,universe,template_id,status,stage,progress,"
                "sections_done,sections_total,words,charts,created_at,finished_at,"
                "interrupted_at,trace_run_id FROM deep_report "
                "WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
                (start, end),
            ).fetchall()
        for row in rows:
            created = str(row["created_at"] or "")
            # 正常写入是本地 naive；万一混入带时区的 ISO，先转北京时间再取日期
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))
                local_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                local_date = created[:10]
            if not local_date.startswith(prefix):
                continue
            report_days.setdefault(local_date, []).append({
                "rid": row["rid"],
                "title": row["title"] or "",
                "universe": row["universe"] or "",
                "template_id": row["template_id"] or "",
                "status": row["status"] or "",
                "stage": row["stage"] or "",
                "progress": row["progress"] or 0,
                "sections_done": row["sections_done"] or 0,
                "sections_total": row["sections_total"] or 0,
                "words": row["words"],
                "charts": row["charts"],
                "created_at": created,
                "finished_at": row["finished_at"] or "",
                "interrupted_at": row["interrupted_at"] or "",
                "trace_run_id": row["trace_run_id"] or "",
            })
    except Exception:
        logger.warning("日历: 查询长报告失败", exc_info=True)
    return report_days


def load_trace_token_stats(storage, dialog_days: dict[str, list]) -> dict[str, dict]:
    """查询每个对话的 token 统计（从 trace 月库，经 store 分库路由）。"""
    trace_token_map: dict[str, dict] = {}
    try:
        from utils.agent_trace.store import store
        from utils.token_usage import normalize_token_usage_dict

        all_uuids = []
        for dialogs in dialog_days.values():
            for dlg in dialogs:
                all_uuids.append(dlg["dialog_uuid"])

        if not all_uuids:
            return trace_token_map

        dialog_run_map = storage.latest_trace_runs_for_dialogs(all_uuids)
        run_ids = list(set(dialog_run_map.values()))
        if not run_ids:
            return trace_token_map

        run_dur_map = {r["id"]: r["duration_ms"] for r in store.get_runs_by_ids(run_ids)}

        token_map: dict[str, dict] = {}
        for rid, items in store.llm_steps_totals(run_ids).items():
            for item in items:
                extra_str = item.get("extra")
                if not extra_str or not isinstance(extra_str, str):
                    continue
                try:
                    d = json.loads(extra_str)
                    tu = normalize_token_usage_dict(d.get("token_usage"))
                    if rid not in token_map:
                        token_map[rid] = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "cost": 0.0, "cost_unpriced": 0, "by_provider": {}}
                    token_map[rid]["llm_calls"] += 1
                    token_map[rid]["input_tokens"] += tu["input_tokens"]
                    token_map[rid]["output_tokens"] += tu["output_tokens"]
                    token_map[rid]["total_tokens"] += tu["total_tokens"]
                    token_map[rid]["cached_tokens"] += tu.get("cached_tokens", 0)
                    token_map[rid]["reasoning_tokens"] += tu.get("reasoning_tokens", 0)
                    # 成本：记录时已算好（含峰谷与价目快照），这里只 SUM；
                    # total=None 表示价目里没有该供应商/模型 —— 单列计数，不按 0 计（否则
                    # 「未计价」会被显示成「不要钱」）
                    # 按供应商：token 五件套 + 成本（与 store.aggregate_llm_usage 同口径）
                    _prov = 'unknown'
                    if isinstance(d.get("cost"), dict):
                        _prov = str((d.get("cost") or {}).get('provider') or 'unknown')
                    elif d.get("provider"):
                        _prov = str(d.get("provider"))
                    _pb = token_map[rid]["by_provider"].setdefault(
                        _prov, {"calls": 0, "in": 0, "out": 0, "cached": 0,
                                "reasoning": 0, "cost": 0.0})
                    _pb["calls"] += 1
                    _pb["in"] += tu["input_tokens"]
                    _pb["out"] += tu["output_tokens"]
                    _pb["cached"] += tu.get("cached_tokens", 0)
                    _pb["reasoning"] += tu.get("reasoning_tokens", 0)

                    cost = d.get("cost")
                    if isinstance(cost, dict):
                        amount = cost.get("total")
                        if amount is None:
                            token_map[rid]["cost_unpriced"] += 1
                        else:
                            token_map[rid]["cost"] += float(amount)
                            token_map[rid]["by_provider"].setdefault(
                                _prov, {"calls": 0, "in": 0, "out": 0, "cached": 0,
                                        "reasoning": 0, "cost": 0.0})["cost"] += float(amount)
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
                "cost": round(tm.get("cost", 0.0), 6),
                "cost_unpriced": tm.get("cost_unpriced", 0),
                "by_provider": tm.get("by_provider", {}),
                "duration_ms": run_dur_map.get(run_id),
            }
    except Exception:
        logger.warning("日历: 查询 trace token 统计失败", exc_info=True)
    return trace_token_map


def build_calendar_payload(storage, year: int, month: int) -> dict:
    """构建日历响应数据。"""
    trading_days = load_trading_days(year, month)
    dialog_days = load_dialog_days(storage, year, month)
    trace_token_map = load_trace_token_stats(storage, dialog_days)
    # 长报告不是对话：单独一列（deep_reports），**不伪造 web_dialogs 记录**
    deep_report_days = load_deep_report_days(year, month)
    # 公司事件图层（解禁/分红/披露/股东大会/预告），与对话记录并列展示
    from server.events_service import EVENT_LABELS, load_corporate_events
    events = load_corporate_events(year, month)

    for dialogs in dialog_days.values():
        for dlg in dialogs:
            stats = trace_token_map.get(dlg["dialog_uuid"], {})
            dlg["llm_calls"] = stats.get("llm_calls", 0)
            dlg["input_tokens"] = stats.get("input_tokens", 0)
            dlg["output_tokens"] = stats.get("output_tokens", 0)
            dlg["total_tokens"] = stats.get("total_tokens", 0)
            dlg["cached_tokens"] = stats.get("cached_tokens", 0)
            dlg["reasoning_tokens"] = stats.get("reasoning_tokens", 0)
            # 成本（元）：这一天/这条记录花了多少（成本 / 计价 §8③）
            dlg["cost"] = stats.get("cost", 0.0)
            dlg["cost_unpriced"] = stats.get("cost_unpriced", 0)
            dlg["by_provider"] = stats.get("by_provider", {})
            dlg["duration_ms"] = stats.get("duration_ms")

    return {"year": year, "month": month, "trading_days": trading_days,
            "dialog_days": dialog_days, "deep_reports": deep_report_days,
            "events": events,
            "event_types": [{"key": k, "label": v} for k, v in EVENT_LABELS.items()]}

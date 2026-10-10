"""A7 质量观测聚合服务：三库（+eval）只读聚合纯函数。

数据源（全部只读连接 file:...?mode=ro，缺库时为 None 并优雅降级，AC-2）：
- signal_eval.db::signal          —— 信号跟踪（北极星口径 / 胜率样本池）
- stock_radar.db::web_messages    —— assistant 报告（mode / scenario 推断源）
- agent_trace.db::runs/steps      —— 成本（steps.extra JSON token_usage）与耗时（runs.duration_ms）
- eval.db::eval_cases             —— LLM 裁判分（overall_score，按 trace_run_id 关联）

JOIN 键终稿（实测验证，docs/spikes/2026-09-07-quality-join-keys.md）：
- signal.dialog_uuid = web_messages.dialog_uuid（同一对话的信号归到该对话的 mode×scenario 格）
- signal.message_uuid = CAST(web_messages.id AS TEXT)（⚠️ 是整数 id 的字符串，不是 message_uuid 列）
- web_messages.trace_run_id = runs.id（实测 100% 覆盖）；mode 与 runs.agent_name 值域一致
- eval_cases.trace_run_id = runs.id（eval 为跑批，与 web 对话基本不交叉 → judge 覆盖率天然低，PRD 已预期）
- 成本：steps.extra JSON 内 token_usage（照 utils/agent_trace/calendar_query.py::_query_token_map 先例）

scenario 双源推断（概要设计 M3 裁决）：
1. dashboard_id 优先（dashboard JSON 埋点，实测已上线：market/sector/stock/screening/hot_events）；
2. react 系 → select_template step output 的 selected_template_id；
3. 全模式兜底 → assistant 消息 dashboard JSON 的 key 签名（candidate_stocks→筛选 /
   index_data→大盘 / price_levels·split_advice→个股），推不出 →「未知」桶。
矩阵响应带 coverage（胜率/评估/场景覆盖率）与 scenario_source 标注，「未知」桶如实展示。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.agent_trace.store import open_merged_readonly
from utils.app_paths import (get_app_dir, get_db_path, get_eval_db_path,
                             get_signal_eval_db_path, get_trace_db_path)
from utils.token_usage import normalize_token_usage_dict

# 北京时间（UTC+8）—— 周边界按北京时间计算（概要设计共享约定 #6 / AC-4）
_BEIJING = timezone(timedelta(hours=8))

# 北极星趋势周数（PRD G2：≥8 周）
_TREND_WEEKS = 8

# dashboard_id（dashboard JSON 埋点）→ 场景（与 dashboards/*.json 的 id 对齐）
_SCENARIO_BY_DASHBOARD_ID: dict[str, str] = {
    "stock": "个股",
    "sector": "板块",
    "market": "大盘",
    "screening": "筛选",
    "hot_events": "热点",
}

# select_template 的 selected_template_id → 场景（react 系历史数据推断源②）
_SCENARIO_BY_TEMPLATE_ID: dict[str, str] = {
    "stock": "个股",
    "valuation_percentile": "个股",
    "sector": "板块",
    "market": "大盘",
    "screening": "筛选",
    "hot_events": "热点",
}

# dashboard JSON key 签名兜底（M3：全模式历史数据推断源③）
_KEY_SIGNATURES: list[tuple[str, str]] = [
    ("candidate_stocks", "筛选"),
    ("index_data", "大盘"),
    ("price_levels", "个股"),
    ("split_advice", "个股"),
]

# dashboard JSON 嵌入标记（chat-dashboard 渲染段，实测 @@DASHBOARD_START@@ ... @@DASHBOARD_END@@）
_DASHBOARD_ID_RE = re.compile(r'"dashboard_id"\s*:\s*"([^"]+)"')


@dataclass
class QualityDBs:
    """四库只读连接（uri mode=ro），缺库/空库时为 None。"""

    signal: sqlite3.Connection | None  # signal_eval.db
    radar: sqlite3.Connection | None   # stock_radar.db
    trace: sqlite3.Connection | None   # agent_trace.db
    eval: sqlite3.Connection | None    # eval.db


def _open_ro(path: str) -> sqlite3.Connection | None:
    """只读打开 SQLite；文件不存在或为空（AC-2 的「缺失/为空」）返回 None。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def open_quality_dbs() -> QualityDBs:
    """打开四库只读连接（业务库统一位于 db/ 子目录；trace 走月库合并视图）。"""
    trace = open_merged_readonly()
    if trace is None:
        trace = _open_ro(get_trace_db_path())   # 兼容旧布局的单文件（可能只有 reviews）
    return QualityDBs(
        signal=_open_ro(get_signal_eval_db_path()),
        radar=_open_ro(get_db_path()),
        trace=trace,
        eval=_open_ro(get_eval_db_path()),
    )


def close_quality_dbs(dbs: QualityDBs) -> None:
    """关闭非 None 连接（幂等）。"""
    for conn in (dbs.signal, dbs.radar, dbs.trace, dbs.eval):
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ── 时间口径（AC-4：周边界按北京时间） ─────────────────────────────


def _parse_iso_utc(value: str | None) -> datetime | None:
    """解析库内 ISO 时间字符串；无时区按 UTC 处理；解析失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        # 兼容 'Z' 后缀等非严格格式
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _week_key_beijing(iso_utc: str) -> str:
    """ISO UTC 字符串 → 北京时间 ISO 周标签（如 'W37'）。周一 00:00（北京时间）换周。"""
    dt = _parse_iso_utc(iso_utc)
    if dt is None:
        return "W00"
    bj = dt.astimezone(_BEIJING)
    iso = bj.isocalendar()
    return f"W{iso.week:02d}"


def _beijing_week_start(now: datetime) -> datetime:
    """给定北京时间所在周的周一 00:00（北京时间）。"""
    bj = now.astimezone(_BEIJING)
    day = bj.date() - timedelta(days=bj.weekday())
    return datetime(day.year, day.month, day.day, tzinfo=_BEIJING)


# ── 成本聚合（照 calendar_query._query_token_map 先例） ────────────


def _run_token_stats(trace_conn: sqlite3.Connection | None, run_ids: list[str]) -> dict[str, dict[str, int]]:
    """按 run_id 聚合 steps.extra JSON 内 token_usage（LLM 步骤才有）。

    返回 {run_id: {llm_calls, input_tokens, output_tokens, total_tokens, ...}}。
    """
    if trace_conn is None or not run_ids:
        return {}
    stats: dict[str, dict[str, int]] = {}
    # 分块 IN 查询，规避 SQLite 变量上限（999）
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = trace_conn.execute(
                f"SELECT run_id, extra FROM steps WHERE run_id IN ({placeholders}) "
                "AND step_type='llm' AND extra IS NOT NULL",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            return stats
        for row in rows:
            run_id, extra = row[0], row[1]
            try:
                data = json.loads(extra)
                usage = normalize_token_usage_dict(data.get("token_usage"))
            except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
                continue
            if run_id not in stats:
                stats[run_id] = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                 "cached_tokens": 0, "total_tokens": 0}
            stats[run_id]["llm_calls"] += 1
            stats[run_id]["input_tokens"] += usage.get("input_tokens", 0)
            stats[run_id]["output_tokens"] += usage.get("output_tokens", 0)
            stats[run_id]["cached_tokens"] += usage.get("cached_tokens", 0)
            stats[run_id]["total_tokens"] += usage.get("total_tokens", 0)
    return stats


# ── scenario 推断（M3 双源 + 兜底） ────────────────────────────────


def _extract_dashboard_json(content: str | None) -> dict[str, Any] | None:
    """从 assistant 消息提取嵌入的 dashboard JSON（@@DASHBOARD_START@@ 段）；失败返回 None。"""
    if not content:
        return None
    start = content.find("@@DASHBOARD_START@@")
    end = content.find("@@DASHBOARD_END@@")
    if start == -1 or end == -1 or end <= start:
        return None
    raw = content[start + len("@@DASHBOARD_START@@"):end].strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _dashboard_keys(content: str | None) -> set[str] | None:
    """dashboard JSON 中「值非空」的 key 集合（key 签名推断用）；无 dashboard 段返回 None。"""
    data = _extract_dashboard_json(content)
    if data is None:
        # 兜底：无成对标记时按 dashboard_id 正则粗判存在性
        return set() if content else None
    return {k for k, v in data.items() if v not in (None, "", [], {})}


def infer_scenario(*, dashboard_id: str | None, template_id: str | None,
                   dashboard_keys: set[str] | None) -> str:
    """scenario 双源推断（M3）。值域：个股/板块/大盘/筛选/热点/未知。

    优先级：dashboard_id 埋点 → select_template 模板 id → dashboard JSON key 签名 → 未知。
    """
    if dashboard_id and dashboard_id in _SCENARIO_BY_DASHBOARD_ID:
        return _SCENARIO_BY_DASHBOARD_ID[dashboard_id]
    if template_id and template_id in _SCENARIO_BY_TEMPLATE_ID:
        return _SCENARIO_BY_TEMPLATE_ID[template_id]
    if dashboard_keys:
        for key, scenario in _KEY_SIGNATURES:
            if key in dashboard_keys:
                return scenario
    return "未知"


# ── 行读取辅助（缺库/缺表/缺列一律降级为空，不抛错） ────────────────


def _fetch_rows(conn: sqlite3.Connection | None, sql: str, params: tuple = ()) -> list[dict]:
    """只读查询；连接缺失/表不存在/查询失败 → 空列表（AC-2 优雅降级）。"""
    if conn is None:
        return []
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _load_signals(signal_conn: sqlite3.Connection | None) -> list[dict]:
    """读 signal 全表（北极星口径 + 胜率样本池）。来源：signal_eval.db::signal。"""
    return _fetch_rows(
        signal_conn,
        "SELECT id, dialog_uuid, message_uuid, symbol_key, decision, confidence, "
        "is_market_wide, signal_time FROM signal ORDER BY signal_time",
    )


def _chunked_in(trace_conn: sqlite3.Connection | None, sql_head: str, ids: list[str]) -> list[dict]:
    """按 run_id 分块 IN 查询（runs/steps 等以 id=TEXT 为键的表）。"""
    if trace_conn is None or not ids:
        return []
    out: list[dict] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        out.extend(_fetch_rows(trace_conn, f"{sql_head} IN ({placeholders})", tuple(chunk)))
    return out


# ── overview（北极星 + 环比 + 胜率 + 成本 + 缓存 + 周趋势） ─────────


def _cache_hit_rate() -> float | None:
    """A8 语义缓存命中率（0-1）。与 /api/llm/cache/stats 同源（AC-7）。

    utils.llm 首次导入会拉起 langchain 依赖链，照 routes/llm_cache.py 的方式
    在调用时懒加载；A8 关闭（灰度默认）或统计失败时返回 None（前端显示 na）。
    """
    try:
        import importlib
        cache_policy = importlib.import_module("utils.llm.policies.cache_policy")
        stats = cache_policy.get_module_stats()
        if not stats.get("enabled"):
            return None
        return stats.get("hit_rate")
    except Exception:
        return None


def build_overview(dbs: QualityDBs, *, now: datetime | None = None,
                   signal_overview: dict | None = None) -> dict:
    """overview 聚合（纯函数）。now 缺省取当前北京时间。

    - verifiable_conclusions：A2 上线后 = 当周 confidence 非空的 signal 条数；
      之前 = 当周 signal 条数（verifiable_basis 如实标注口径）；
    - win_rate_after_cost：signal_overview 提供（signal-eval 费后收益统计）时接入
      value=buy_winrate（0-1）、wilson=buy_ci，coverage 标注口径；未提供时 value=null；
      n 恒为 buy 类可统计样本数；
    - avg_cost_per_report：近 28 天 assistant 报告的 run 级 token 聚合均值（unit=tokens，
      无单价配置时按 token 展示）；无数据 → null；
    - cache_hit_rate：A8 语义缓存命中率（0-1）；A8 未上线 → null；
    - token_cache_hit_rate：本轮 trace token 聚合的 cached/total（0-1），无数据 → null。
    """
    now_bj = (now or datetime.now(timezone.utc)).astimezone(_BEIJING)
    week_start = _beijing_week_start(now_bj)
    prev_week_start = week_start - timedelta(days=7)
    week_label = f"W{week_start.isocalendar().week:02d}"

    signals = _load_signals(dbs.signal)
    # 信号时间 → 北京时间（signal_time 为 ISO UTC 字符串，实测 108 行全部可解析）
    parsed: list[tuple[datetime | None, dict]] = [
        (_parse_iso_utc(s.get("signal_time")), s) for s in signals
    ]

    # 口径：confidence 非空（A2 修复后）→ with_confidence；否则降级为 tracked（Q2 认可）
    has_confidence = any(s.get("confidence") is not None for _, s in parsed)
    verifiable_basis = "with_confidence" if has_confidence else "tracked"

    def _in_week(start: datetime, end: datetime) -> int:
        return sum(
            1 for dt, s in parsed
            if dt is not None and start <= dt < end
            and (not has_confidence or s.get("confidence") is not None)
        )

    verifiable = _in_week(week_start, week_start + timedelta(days=7))
    prev_week = _in_week(prev_week_start, week_start)
    wow_pct = round((verifiable - prev_week) / prev_week * 100) if prev_week > 0 else None

    # 周趋势：近 8 周（含当周），固定周标签补零（G2：≥8 周）
    trend = []
    for i in range(_TREND_WEEKS - 1, -1, -1):
        start = week_start - timedelta(weeks=i)
        end = start + timedelta(days=7)
        trend.append({
            "week": f"W{start.isocalendar().week:02d}",
            "count": _in_week(start, end),
            "start": start.strftime("%m-%d"),
            "end": (end - timedelta(seconds=1)).strftime("%m-%d"),
        })

    # 胜率样本池：buy 类、已补标 symbol_key、非大盘类（与 signal_eval._load_signals 口径一致）
    win_pool = [
        s for _, s in parsed
        if s.get("decision") == "buy" and s.get("symbol_key") and not s.get("is_market_wide")
    ]

    # 单报告平均成本：近 28 天 assistant 报告 → run 级 token 聚合（来源：agent_trace.steps.extra）
    cost_start = now_bj - timedelta(days=28)
    msgs = _fetch_rows(
        dbs.radar,
        "SELECT id, trace_run_id, created_at FROM web_messages "
        "WHERE role='assistant' AND trace_run_id IS NOT NULL",
    )
    recent_run_ids = [
        m["trace_run_id"] for m in msgs
        if m.get("trace_run_id") and (_parse_iso_utc(m.get("created_at")) or now_bj) >= cost_start
    ]
    token_map = _run_token_stats(dbs.trace, recent_run_ids)
    totals = [v["total_tokens"] for v in token_map.values() if v.get("total_tokens")]
    avg_cost = round(sum(totals) / len(totals)) if totals else None
    cached_sum = sum(v.get("cached_tokens", 0) for v in token_map.values())
    token_sum = sum(v.get("total_tokens", 0) for v in token_map.values())
    token_cache_hit_rate = round(cached_sum / token_sum, 4) if token_sum > 0 else None

    # 费后胜率：signal-eval 费后收益统计接入（未提供 → 保持占位口径）
    if signal_overview:
        win_value = signal_overview.get("buy_winrate")
        win_wilson = signal_overview.get("buy_ci")
        win_coverage = "口径：signal-eval 费后收益统计（单边成本 0）"
    else:
        win_value = None
        win_wilson = None
        win_coverage = "待接入收益口径：已跟踪 buy 类信号样本，费后胜率随 signal-eval 统计开启"

    return {
        "week": week_label,
        "verifiable_conclusions": verifiable if dbs.signal is not None else None,
        "verifiable_basis": verifiable_basis,
        "wow_pct": wow_pct,
        "prev_week_count": prev_week,
        "win_rate_after_cost": {
            "value": win_value,
            "n": len(win_pool),
            "wilson": win_wilson,
            "coverage": win_coverage,
        },
        "avg_cost_per_report": {
            "value": avg_cost,
            "unit": "tokens",
            "n": len(totals),
        },
        "cache_hit_rate": _cache_hit_rate(),
        "token_cache_hit_rate": token_cache_hit_rate,
        "trend": trend if dbs.signal is not None else [],
    }


# ── matrix（模式 × 场景） ──────────────────────────────────────────

_MODE_ORDER = ["unified_plan", "react_stock", "agent_group", "pdor"]
_SCENARIO_ORDER = ["个股", "板块", "大盘", "筛选", "热点", "未知"]
_MIN_COLOR_N = 5  # AC-3：n<5 无色阶


def _cell_color(n: int, win_rate: float | None, judge_avg: float | None) -> str:
    """单元格色阶：n<5 → 'na'（灰底无色阶，AC-3）；胜率 0.6/0.4 分档；裁判分 7/5 分档；无可比指标 → 'na'。"""
    if n < _MIN_COLOR_N:
        return "na"
    if win_rate is not None:
        return "good" if win_rate >= 0.6 else ("bad" if win_rate <= 0.4 else "mid")
    if judge_avg is not None:
        return "good" if judge_avg >= 7 else ("bad" if judge_avg < 5 else "mid")
    return "na"


def build_matrix(dbs: QualityDBs, *, dim: str = "mode_scenario", days: int = 90,
                 now: datetime | None = None) -> dict:
    """模式 × 场景矩阵（纯函数）。dim 目前仅支持 mode_scenario（一期单一维度）。

    每格数据来源（AC-1 可追溯）：
    - n：stock_radar.web_messages（role='assistant'，created_at 近 days 天，北京时间窗口）
    - mode：web_messages.mode（缺省回退 agent_trace.runs.agent_name，值域一致）
    - scenario：infer_scenario（dashboard_id → select_template 模板 → key 签名）
    - win_rate：signal_eval.signal（dialog_uuid 关联）+ K 线收益口径 —— 本期 null，win_n 记可归因样本
    - judge_avg/judge_n：eval.db::eval_cases.overall_score（trace_run_id = runs.id 关联）
    - avg_cost：agent_trace.steps.extra → token_usage 聚合均值（unit=tokens）
    - avg_latency_s：agent_trace.runs.duration_ms / 1000 均值
    """
    now_bj = (now or datetime.now(timezone.utc)).astimezone(_BEIJING)
    cutoff = now_bj - timedelta(days=days)

    msgs = _fetch_rows(
        dbs.radar,
        "SELECT id, dialog_uuid, trace_run_id, mode, content, created_at FROM web_messages "
        "WHERE role='assistant'",
    )
    msgs = [
        m for m in msgs
        if (_parse_iso_utc(m.get("created_at")) or now_bj) >= cutoff
    ]

    run_ids = sorted({m["trace_run_id"] for m in msgs if m.get("trace_run_id")})
    # runs：agent_name 兜底 + duration_ms（来源：agent_trace.runs）
    run_map: dict[str, dict] = {
        r["id"]: r for r in _chunked_in(dbs.trace, "SELECT id, agent_name, duration_ms FROM runs WHERE id", run_ids)
    }
    # select_template 模板 id（react 系场景推断源②，来源：agent_trace.steps.output）
    tpl_map: dict[str, str] = {}
    for row in _chunked_in(dbs.trace, "SELECT run_id, output FROM steps WHERE step_name='select_template' AND run_id", run_ids):
        try:
            data = json.loads(row.get("output") or "{}")
            tid = data.get("selected_template_id")
        except (json.JSONDecodeError, TypeError, AttributeError):
            tid = None
        if tid and row.get("run_id"):
            tpl_map[row["run_id"]] = str(tid)

    token_map = _run_token_stats(dbs.trace, run_ids)
    # LLM 裁判分（来源：eval.db::eval_cases.overall_score，trace_run_id 关联）
    judge_map: dict[str, list[float]] = {}
    for row in _fetch_rows(dbs.eval, "SELECT trace_run_id, overall_score FROM eval_cases WHERE overall_score IS NOT NULL"):
        if row.get("trace_run_id"):
            judge_map.setdefault(row["trace_run_id"], []).append(float(row["overall_score"]))

    # signal 归组：dialog_uuid → 该对话矩阵格（胜率可归因样本，本期只计 n 不算收益）
    msg_by_dialog: dict[str, dict] = {}
    for m in msgs:
        if m.get("dialog_uuid") and m["dialog_uuid"] not in msg_by_dialog:
            msg_by_dialog[m["dialog_uuid"]] = m
    window_signals = [
        s for s in _load_signals(dbs.signal)
        if (dt := _parse_iso_utc(s.get("signal_time"))) is not None and dt >= cutoff
    ]

    cells: dict[tuple[str, str], dict] = {}
    scenario_source = {"dashboard_id": 0, "template_id": 0, "key_signature": 0, "unknown": 0}
    total_msgs = len(msgs)
    known_scenario = 0

    for m in msgs:
        run = run_map.get(m.get("trace_run_id") or "", {})
        mode = m.get("mode") or run.get("agent_name") or "unknown"
        # scenario 推断并记录来源
        m_dashboard_id = None
        if m.get("content"):
            mid = _DASHBOARD_ID_RE.search(m["content"])
            m_dashboard_id = mid.group(1) if mid else None
        m_template_id = tpl_map.get(m.get("trace_run_id") or "")
        keys = _dashboard_keys(m.get("content"))
        if m_dashboard_id and m_dashboard_id in _SCENARIO_BY_DASHBOARD_ID:
            source = "dashboard_id"
        elif m_template_id:
            source = "template_id"
        elif keys and any(k in keys for k, _ in _KEY_SIGNATURES):
            source = "key_signature"
        else:
            source = "unknown"
        scenario = infer_scenario(dashboard_id=m_dashboard_id, template_id=m_template_id, dashboard_keys=keys)
        scenario_source[source] += 1
        if scenario != "未知":
            known_scenario += 1

        key = (mode, scenario)
        cell = cells.setdefault(key, {
            "mode": mode, "scenario": scenario, "n": 0, "win_rate": None, "win_n": 0,
            "judge_avg": None, "judge_n": 0, "avg_cost": None, "avg_latency_s": None,
            "_tokens": [], "_latencies": [],
        })
        cell["n"] += 1
        rid = m.get("trace_run_id") or ""
        if rid in judge_map:
            scores = judge_map[rid]
            cell["judge_n"] += 1
            prev = cell["judge_avg"] or 0.0
            cell["judge_avg"] = round((prev * (cell["judge_n"] - 1) + sum(scores) / len(scores)) / cell["judge_n"], 2)
        if rid in token_map and token_map[rid].get("total_tokens"):
            cell["_tokens"].append(token_map[rid]["total_tokens"])
        duration = run.get("duration_ms")
        if duration is not None:
            cell["_latencies"].append(float(duration) / 1000.0)

    # signal 可归因样本：signal.dialog_uuid → 命中窗口内消息 → 归到该对话的格
    attributed = 0
    for s in window_signals:
        m = msg_by_dialog.get(s.get("dialog_uuid") or "")
        if m is None:
            continue
        run = run_map.get(m.get("trace_run_id") or "", {})
        mode = m.get("mode") or run.get("agent_name") or "unknown"
        mid = _DASHBOARD_ID_RE.search(m["content"]) if m.get("content") else None
        scenario = infer_scenario(
            dashboard_id=mid.group(1) if mid else None,
            template_id=tpl_map.get(m.get("trace_run_id") or ""),
            dashboard_keys=_dashboard_keys(m.get("content")),
        )
        cell = cells.get((mode, scenario))
        if cell is not None and s.get("decision") == "buy":
            cell["win_n"] += 1
        attributed += 1

    out_cells = []
    for (mode, scenario), cell in sorted(cells.items(), key=lambda kv: (
        _MODE_ORDER.index(kv[0][0]) if kv[0][0] in _MODE_ORDER else len(_MODE_ORDER),
        _SCENARIO_ORDER.index(kv[0][1]) if kv[0][1] in _SCENARIO_ORDER else len(_SCENARIO_ORDER),
    )):
        tokens = cell.pop("_tokens")
        latencies = cell.pop("_latencies")
        cell["avg_cost"] = round(sum(tokens) / len(tokens)) if tokens else None
        cell["avg_latency_s"] = round(sum(latencies) / len(latencies), 1) if latencies else None
        cell["color"] = _cell_color(cell["n"], cell["win_rate"], cell["judge_avg"])
        out_cells.append(cell)

    total_signals = len(window_signals)
    return {
        "dims": ["mode", "scenario"],
        "dim": dim,
        "days": days,
        "cells": out_cells,
        "coverage": {
            # 胜率覆盖 = 窗口内可归因到矩阵格的 signal 占比（收益口径本期未接入，value 仍为 null）
            "win_rate": round(attributed / total_signals, 2) if total_signals else None,
            "judge_score": round(sum(1 for m in msgs if (m.get("trace_run_id") or "") in judge_map) / total_msgs, 2)
            if total_msgs else None,
            "scenario": round(known_scenario / total_msgs, 2) if total_msgs else None,
        },
        "scenario_source": scenario_source,
        "note": "win_rate 本期未接入 K 线收益口径（signal-eval 统计就绪后开启）；judge 分来自 eval 跑批，与 web 对话基本不交叉，覆盖率天然偏低（PRD 已预期）",
    }

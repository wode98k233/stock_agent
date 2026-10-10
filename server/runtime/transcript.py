"""Transcript 条目构造器 — SSE 增量事件与快照接口共用一份逻辑（同构）。

背景（docs/superpowers/specs/2026-10-10-agent-transcript-ui-design.md）：
对话过程流 UI 需要两类条目——think（`messages.reasoning`）与工具调用（`steps`）。
- 增量：`trace_poller` 轮询 diff 后推 SSE `transcript` 事件；
- 快照：`GET /api/tasks/{id}/transcript` 全量返回（运行中刷新/切回恢复用）；
- 回放：任务落库时本模块全量条目并入 `web_messages.extra`。
三处共用 `build_*_entry` 构造，`entry_id` 幂等（think=`t:<message_id>`、tool=`u:<step_id>`），
前端按 entry_id upsert；条目带 `type: "transcript"` 与 ProgressEvent 数组混存兼容。
"""
from __future__ import annotations

import json

# 摘要上限：事件 payload 不携带全文，工具结果全文由前端懒加载 getStepDetail
INPUT_SUMMARY_CHARS = 200
OUTPUT_SUMMARY_CHARS = 300

# steps.status → 条目状态（trace 侧 success/error/running → UI 侧 ok/error/running）
_STATUS_MAP = {"success": "ok", "error": "error", "running": "running"}


def summarize(text, limit: int) -> str:
    """截断为单行摘要：None 容忍、超限加省略号、JSON 对象转紧凑字符串。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _entry_version(entry: dict) -> str:
    """条目版本：同 entry_id 内容变化（running→ok/error）时必须重推。"""
    return json.dumps(
        {
            "status": entry.get("status"),
            "duration_ms": entry.get("duration_ms"),
            "error": entry.get("error"),
            "text_len": len(entry.get("text") or ""),
            "out_len": len(entry.get("output_summary") or ""),
        },
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    )


def build_tool_entry(step: dict) -> dict | None:
    """steps 行 → 工具条目（仅 step_type='tool'）。"""
    if not step or step.get("step_type") != "tool":
        return None
    sid = step.get("id")
    if sid is None:
        return None
    status = _STATUS_MAP.get(step.get("status") or "running", "ok")
    error = step.get("error")
    return {
        "type": "transcript",
        "entry_id": f"u:{sid}",
        "kind": "tool",
        "run_id": step.get("run_id"),
        "sort": [int(sid), 0],
        "step_id": int(sid),
        "tool_name": step.get("step_name") or "tool",
        "input_summary": summarize(step.get("input"), INPUT_SUMMARY_CHARS),
        "output_summary": summarize(step.get("output"), OUTPUT_SUMMARY_CHARS),
        "status": status,
        "duration_ms": step.get("duration_ms"),
        "error": summarize(error, 120) if error else None,
    }


def build_think_entry(message: dict) -> dict | None:
    """messages 行（reasoning 非空）→ think 条目。"""
    if not message:
        return None
    text = message.get("reasoning")
    if not text or not str(text).strip():
        return None
    mid = message.get("id")
    if mid is None:
        return None
    sid = int(message.get("step_id") or 0)
    return {
        "type": "transcript",
        "entry_id": f"t:{mid}",
        "kind": "think",
        "run_id": message.get("run_id"),
        "sort": [sid, int(mid)],
        "text": str(text),
    }


def read_run_entries(conn, run_id: str) -> list[dict]:
    """快照：一次读全 run 的 think + 工具条目，按 sort 排序。"""
    if conn is None or not run_id:
        return []
    entries: list[dict] = []
    for row in conn.execute(
        "SELECT id, run_id, step_type, step_name, input, output, status, duration_ms, error "
        "FROM steps WHERE run_id=? AND step_type='tool' ORDER BY id",
        (run_id,),
    ).fetchall():
        entry = build_tool_entry(dict(row))
        if entry:
            entries.append(entry)
    for row in conn.execute(
        "SELECT m.id, m.step_id, m.reasoning, s.run_id "
        "FROM messages m JOIN steps s ON s.id = m.step_id "
        "WHERE s.run_id=? AND m.reasoning IS NOT NULL AND TRIM(m.reasoning) != '' "
        "ORDER BY m.id",
        (run_id,),
    ).fetchall():
        entry = build_think_entry(dict(row))
        if entry:
            entries.append(entry)
    entries.sort(key=lambda e: tuple(e.get("sort") or (0, 0)))
    return entries


def changed_entries(conn, run_id: str, seen: dict) -> list[dict]:
    """增量：按 entry_id+版本过滤已推送条目（同 entry_id 状态变化会重推）。

    `seen` 由调用方（trace_poller）按任务持有，形如 {entry_id: version}。
    """
    changed = []
    for entry in read_run_entries(conn, run_id):
        version = _entry_version(entry)
        if seen.get(entry["entry_id"]) == version:
            continue
        seen[entry["entry_id"]] = version
        changed.append(entry)
    return changed


def entries_for_task(trace_run_id: str | None = None) -> list[dict]:
    """按 run_id 取全量条目；run_id 未知时回退「当前运行中的 run」（与 trace_poller 同口径）。"""
    from utils.agent_trace.store import store

    run_id = trace_run_id
    if not run_id:
        run = store.find_latest_run(status="running")
        run_id = run["id"] if run else None
    if not run_id:
        return []
    conn = store.conn_for_run(run_id)
    return read_run_entries(conn, run_id)

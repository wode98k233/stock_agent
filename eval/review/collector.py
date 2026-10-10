"""run 级评审数据收集：把 trace 的步骤折叠成评审单元。"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_UNIT_CHARS = 1200
MAX_TOTAL_CHARS = 24000
MAX_UNITS = 40

_SKIP_STEP_TYPES = {"agent", "chain"}
_SELECTOR_NODES = {"skill-selector", "select_skills"}


def _loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _truncate(value: Any, limit: int | None = None) -> str:
    if limit is None:
        limit = MAX_UNIT_CHARS
    text = _as_text(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[截断,原长{len(text)}]"


def _omitted_note(count: int) -> str:
    return f"其余 {count} 个步骤已省略"


def _omitted_unit(count: int) -> dict:
    return {"kind": "omitted", "note": _omitted_note(count)}


def _omitted_count(note: Any) -> int:
    m = re.search(r"\d+", str(note or ""))
    return int(m.group()) if m else 1


def _skill_unit(step) -> dict:
    extra = _loads(step["extra"]) or {}
    return {
        "kind": "skill",
        "step_ref": step["id"],
        "event": step["step_name"] or extra.get("kind") or "select",
        "selected": extra.get("selected") or [],
        "reason": _truncate(extra.get("reason") or "", 600),
        "market": extra.get("market") or "",
        "tools": extra.get("tools") or [],
        "active_skill": extra.get("active_skill") or "",
        "trigger": _truncate(extra.get("trigger") or "", 300),
    }


def _think_unit(conn, step) -> dict | None:
    extra = _loads(step["extra"]) or {}
    row = conn.execute(
        "SELECT tool_calls, reasoning FROM messages "
        "WHERE step_id=? AND role='ai' ORDER BY seq DESC LIMIT 1",
        (step["id"],),
    ).fetchone()
    reasoning = (row["reasoning"] or "") if row else ""
    calls = []
    if row:
        for tc in (_loads(row["tool_calls"]) or []):
            if isinstance(tc, dict) and tc.get("name"):
                calls.append({"name": tc["name"], "args": _truncate(tc.get("args") or {}, 300)})
    if not reasoning and not calls:
        return None
    return {
        "kind": "think",
        "step_ref": step["id"],
        "label": extra.get("label") or step["step_name"] or "LLM",
        "reasoning": _truncate(reasoning),
        "tool_calls": calls,
    }


def _tool_unit(step) -> dict:
    return {
        "kind": "tool",
        "step_ref": step["id"],
        "tool": step["step_name"] or "unknown",
        "args": _truncate(_loads(step["input"]) or "", 400),
        "status": step["status"] or "unknown",
        "result": _truncate(_loads(step["output"]) or "", MAX_UNIT_CHARS),
        "error": _truncate(step["error"] or "", 300),
    }


def _skill_event_fallback(conn, steps) -> dict | None:
    """老 trace 无 skill 事件时，从技能选择节点（skill-selector/select_skills）的 LLM 输出解析。"""
    from utils.llm_factory import extract_json

    for step in steps:
        if (step["step_type"] or "") != "llm":
            continue
        extra = _loads(step["extra"]) or {}
        if extra.get("node") not in _SELECTOR_NODES:
            continue
        rows = conn.execute(
            "SELECT content FROM messages WHERE step_id=? AND role='ai' ORDER BY seq",
            (step["id"],),
        ).fetchall()
        for row in rows:
            content = row["content"]
            data = extract_json(content) if isinstance(content, str) else None
            if data is None:
                data = _loads(content)
            if isinstance(data, dict) and "selected_skills" in data:
                return {
                    "kind": "skill",
                    "step_ref": step["id"],
                    "event": "select",
                    "selected": data.get("selected_skills") or [],
                    "reason": _truncate(data.get("reason") or "", 600),
                    "market": "",
                    "tools": [],
                    "active_skill": "",
                    "trigger": "",
                    "degraded": True,
                }
    return None


def _trim_units_to_budget(digest: dict) -> None:
    """从尾部裁剪 units 直到整体 JSON 不超 MAX_TOTAL_CHARS，末位保留 omitted 提示。

    先写入 truncated 键，确保以下尺寸计算包含它。
    run/skill_events 本身已超限的极端预算下无法保证总长，此时只尽量裁剪。
    """
    digest["truncated"] = True
    units = digest["units"]
    omitted = 0
    if units and units[-1].get("kind") == "omitted":
        omitted = _omitted_count(units.pop().get("note"))
    while units and len(_as_text(digest)) > MAX_TOTAL_CHARS:
        units.pop()
        omitted += 1
    if omitted:
        units.append(_omitted_unit(omitted))
        while len(units) > 1 and len(_as_text(digest)) > MAX_TOTAL_CHARS:
            units.pop(-2)
            omitted += 1
            units[-1]["note"] = _omitted_note(omitted)


def collect_run(conn, run_id: str) -> dict:
    """读取 trace run 并输出评审 digest；run 不存在时抛 ValueError。

    契约（分库后）：conn 必须是**该 run 所在月库**的连接
    （生产侧经 `utils.agent_trace.store.store.conn_for_run(run_id)` 获取），
    row_factory=sqlite3.Row（否则 row["col"] 会报 TypeError）。
    示例：
        conn = store.conn_for_run(run_id)
        digest = collect_run(conn, run_id)
    """
    if conn is None:
        raise ValueError(f"trace run 不存在: {run_id}")
    run = conn.execute(
        "SELECT id, agent_name, input, output, status, error, created_at, duration_ms "
        "FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"trace run 不存在: {run_id}")

    steps = conn.execute(
        "SELECT id, step_type, step_name, status, error, input, output, extra "
        "FROM steps WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()

    units = []
    for step in steps:
        step_type = step["step_type"] or ""
        if step_type in _SKIP_STEP_TYPES:
            continue
        if step_type == "skill":
            unit = _skill_unit(step)
        elif step_type == "llm":
            unit = _think_unit(conn, step)
        elif step_type == "tool":
            unit = _tool_unit(step)
        else:
            unit = None
        if unit:
            units.append(unit)

    if len(units) > MAX_UNITS:
        units = units[:MAX_UNITS] + [_omitted_unit(len(units) - MAX_UNITS)]

    skill_events = [u for u in units if u["kind"] == "skill"]
    if not skill_events:
        fallback = _skill_event_fallback(conn, steps)
        if fallback:
            units.insert(0, fallback)
            skill_events = [fallback]

    digest = {
        "run": {
            "run_id": run["id"],
            "agent_name": run["agent_name"],
            "status": run["status"],
            "created_at": run["created_at"],
            "duration_ms": run["duration_ms"],
            "error": _truncate(run["error"] or "", 400),
            "question": _truncate(_loads(run["input"]) or "", 2000),
            "final_answer": _truncate(_loads(run["output"]) or "", 4000),
        },
        "skill_events": skill_events,
        "units": units,
    }
    if len(_as_text(digest)) > MAX_TOTAL_CHARS:
        _trim_units_to_budget(digest)
    return digest


# dialog digest 携带原始轮次内容（治 R4 的「二手信息」）：回答不再压到 800 字，
# dialog 整体预算也独立放宽（run digest 维持 MAX_TOTAL_CHARS 不变）
MAX_TURN_ANSWER_CHARS = 2000
MAX_DIALOG_TOTAL_CHARS = 48000
MAX_TURN_FINDINGS = 3


def build_turn_summary(turn: dict, review: dict | None = None,
                       skills_used: list | None = None,
                       review_status: str = "", error: str = "") -> dict:
    """把一轮问答与其 run 评审折成综合评审输入。"""
    findings = []
    if review and isinstance(review.get("findings"), list):
        for item in review["findings"][:MAX_TURN_FINDINGS]:
            if isinstance(item, dict):
                findings.append({
                    "step_ref": item.get("step_ref", 0),
                    "dimension": item.get("dimension", ""),
                    "verdict": _truncate(item.get("verdict") or "", 200),
                })
    return {
        "turn_index": turn.get("turn_index"),
        "question": _truncate(turn.get("question") or "", 600),
        "answer_excerpt": _truncate(turn.get("answer") or "", MAX_TURN_ANSWER_CHARS),
        "run_id": turn.get("run_id") or "",
        "review_id": (review or {}).get("review_uuid") or "",
        "review_status": review_status or (review or {}).get("status") or "no_trace",
        "run_overall": (review or {}).get("overall"),
        "run_dimensions": (review or {}).get("scores") or {},
        "top_findings": findings,
        "skills_used": skills_used or [],
        "error": _truncate(error or "", 300),
    }


def build_dialog_digest(dialog: dict, turn_summaries: list[dict],
                        cross_turn: dict | None = None) -> dict:
    """组装 dialog 综合评审 digest，并保证整体不超 MAX_DIALOG_TOTAL_CHARS。

    `cross_turn` 为 `eval.evidence.cross_turn.summarize_cross_turn(turns)` 的
    结果（重复调用 / 跨轮数字矛盾 / 否定约束违规），由引擎组装后传入。
    """
    digest = {
        "dialog": {
            "dialog_uuid": dialog.get("dialog_uuid"),
            "title": _truncate(dialog.get("title") or "", 200),
            "turn_count": len(turn_summaries),
        },
        "turns": list(turn_summaries),
    }
    if cross_turn is not None:
        digest["cross_turn"] = cross_turn
    if len(_as_text(digest)) > MAX_DIALOG_TOTAL_CHARS:
        digest["truncated"] = True
        omitted = {"kind": "omitted", "note": "为保证评审上下文长度，后续轮次已省略"}
        while len(digest["turns"]) > 1:
            digest["turns"].pop()
            if len(_as_text({**digest, "turns": digest["turns"] + [omitted]})) <= MAX_DIALOG_TOTAL_CHARS:
                break
        digest["turns"].append(omitted)
        while len(digest["turns"]) > 1 and len(_as_text(digest)) > MAX_DIALOG_TOTAL_CHARS:
            digest["turns"].pop(-2)
    return digest

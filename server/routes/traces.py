"""Trace 查询路由：/api/traces..., /api/dialogs/{uuid}/trace。"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from server.deps import get_web_state
from server.constants import TRACE_ICONS
from utils.agent_trace.db import get_trace_conn

logger = logging.getLogger(__name__)

router = APIRouter()


def query_trace_steps(run_id: str) -> dict:
    """查询指定 run 的所有 steps。"""
    conn = get_trace_conn()
    if not conn:
        return {"items": []}
    try:
        rows = conn.execute(
            "SELECT id, run_id, event_run_id, parent_run_id, step_type, step_name, "
            "status, duration_ms, extra "
            "FROM steps WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        items = []
        for row in rows:
            step = dict(row)
            step["icon"] = TRACE_ICONS.get(step.get("step_type", ""), "·")
            if step.get("extra") and isinstance(step["extra"], str):
                try:
                    step["extra"] = json.loads(step["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            items.append(step)
        return {"items": items}
    except Exception:
        return {"items": []}


def query_step_messages(conn, step_id: int) -> list:
    """查询指定 step 的消息列表。"""
    try:
        rows = conn.execute(
            "SELECT role, content, tool_calls, tool_call_id, reasoning FROM messages WHERE step_id=? ORDER BY seq",
            (step_id,),
        ).fetchall()
        msgs = []
        for row in rows:
            m = dict(row)
            if m.get("tool_calls") and isinstance(m["tool_calls"], str):
                try:
                    m["tool_calls"] = json.loads(m["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            msgs.append(m)
        return msgs
    except Exception:
        return []


# 里程碑节点中文标签（面向用户的执行回放）
_CHAIN_MILESTONE_LABELS = {
    "classifier": "意图分类",
    "planner": "执行规划",
    "graph": "图谱构建",
    "tools": "工具调度",
    "unified_executor": "统一执行",
    "report_enhance": "报告增强",
    "dashboard": "仪表盘生成",
    "agent": "Agent 决策",
    "run": "执行开始",
}


def _chain_tool_summary(s: dict) -> dict:
    """从工具步骤的 input/output 提炼一行证据摘要（与 storage._tool_step_summary 同款逻辑，避免跨模块耦合）。"""
    def _load(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return {"_raw": str(v)[:200]}
        return v or {}

    inp = _load(s.get("input"))
    out = _load(s.get("output"))
    query = inp.get("query") if isinstance(inp, dict) and inp.get("query") else ""
    if not query and isinstance(inp, dict) and inp.get("_raw"):
        query = str(inp["_raw"])[:120]

    summary = ""
    if isinstance(out, dict):
        if out.get("status") == "failed" or out.get("error"):
            summary = f"查询失败：{str(out.get('error'))[:60]}"
        else:
            tables = out.get("tables") or []
            if tables:
                parts = []
                for t in tables[:3]:
                    name = (t.get("sheet_name") or "").strip()
                    n = len(t.get("rows") or [])
                    parts.append(f"{name}×{n}行" if name else f"{n}行")
                tail = "" if len(tables) <= 3 else f" 等{len(tables)}张表"
                summary = f"{len(tables)}张表{('·' + str(out.get('total_rows')) + '行') if out.get('total_rows') else ''}：" + "、".join(parts) + tail
            elif out.get("_raw"):
                summary = str(out["_raw"])[:80]
    elif isinstance(out, dict) and out.get("_raw"):
        summary = str(out["_raw"])[:80]

    return {
        "step_id": s.get("id"),
        "query": str(query)[:160],
        "summary": str(summary)[:200],
        "status": s.get("status") or "unknown",
    }


def query_trace_chain(run_id: str) -> dict:
    """把 run 的 steps + messages 折叠成「思考链路」：面向用户的执行回放。

    用户看到的是最终报告，这条链展示它怎么一步步得出来的：
      - think:      LLM 的思考过程（reasoning）+ 发起的工具调用（tool_calls）
      - tool:       工具执行（查询语句 + 结果摘要）
      - milestone:  阶段节点（意图分类/执行规划/报告增强…）
    按 step id 升序天然为时间序（决策步骤先于其工具执行步骤）。
    """
    conn = get_trace_conn()
    if not conn:
        return {"items": []}
    try:
        steps = conn.execute(
            "SELECT id, step_type, step_name, status, duration_ms, input, output "
            "FROM steps WHERE run_id=? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        items: list[dict] = []
        for row in steps:
            s = dict(row)
            st = s["step_type"] or ""
            name = s["step_name"] or ""

            if st == "llm":
                # LLM 决策步骤：取 ai 消息的 reasoning + tool_calls（system/human/tool 消息不展示）
                msgs = conn.execute(
                    "SELECT reasoning, tool_calls FROM messages "
                    "WHERE step_id=? AND role='ai' ORDER BY seq ASC",
                    (s["id"],),
                ).fetchall()
                for m in msgs:
                    reasoning = (m["reasoning"] or "").strip()
                    tcs = m["tool_calls"]
                    if isinstance(tcs, str):
                        try:
                            tcs = json.loads(tcs)
                        except (json.JSONDecodeError, TypeError):
                            tcs = None
                    calls = []
                    for tc in (tcs or []):
                        if isinstance(tc, dict) and tc.get("name"):
                            args = tc.get("args") or {}
                            if isinstance(args, dict) and args.get("query"):
                                args = {"query": str(args["query"])[:160]}
                            calls.append({"name": tc["name"], "args": args})
                    if not reasoning and not calls:
                        continue
                    items.append({
                        "type": "think",
                        "step_id": s["id"],
                        "step_name": name,
                        "reasoning": reasoning[:4000],
                        "tool_calls": calls,
                        "status": s["status"] or "unknown",
                    })
            elif st == "tool":
                # 工具执行步骤
                items.append({
                    "type": "tool",
                    "step_id": s["id"],
                    "step_name": name,
                    **_chain_tool_summary(s),
                })
            elif st == "agent":
                # Agent 容器步骤：跳过（内部已由 think/tool 节点体现，避免 40+ 个重复里程碑噪音）
                continue
            elif st in ("classifier", "planner", "unified_executor", "report_enhance", "dashboard"):
                items.append({
                    "type": "milestone",
                    "step_id": s["id"],
                    "label": _CHAIN_MILESTONE_LABELS.get(st, name or st),
                    "status": s["status"] or "unknown",
                    "duration_ms": s["duration_ms"],
                })
        return {"items": items, "count": len(items)}
    except Exception:
        return {"items": [], "count": 0}


def find_trace_run_by_input(user_input: str, approx_created_at: str | None = None) -> str | None:
    """按用户输入内容查找最近的 trace run。"""
    conn = get_trace_conn()
    if not conn:
        return None
    try:
        encoded = json.dumps(user_input)
        if approx_created_at:
            row = conn.execute(
                "SELECT id FROM runs WHERE input=? AND created_at<=? "
                "ORDER BY created_at DESC LIMIT 1",
                (encoded, approx_created_at),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM runs WHERE input=? "
                "ORDER BY created_at DESC LIMIT 1",
                (encoded,),
            ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


@router.get("/api/traces/runs/{run_id}/steps")
async def trace_steps(run_id: str):
    return query_trace_steps(run_id)


@router.get("/api/traces/runs/{run_id}/chain")
async def trace_chain(run_id: str):
    """思考链路：面向用户的执行回放（reasoning / 工具调用 / 里程碑）。"""
    return query_trace_chain(run_id)


@router.get("/api/traces/steps/{step_id}/detail")
async def trace_step_detail(step_id: int):
    conn = get_trace_conn()
    if not conn:
        return {"input": None, "output": None, "messages": []}
    try:
        row = conn.execute(
            "SELECT input, output FROM steps WHERE id=?",
            (step_id,),
        ).fetchone()
        if not row:
            return {"input": None, "output": None, "messages": []}
        result = {}
        for field in ("input", "output"):
            val = row[field]
            if val and isinstance(val, str):
                try:
                    result[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    result[field] = val
            else:
                result[field] = val
        result["messages"] = query_step_messages(conn, step_id)
        return result
    except Exception:
        return {"input": None, "output": None, "messages": []}


@router.get("/api/dialogs/{dialog_uuid}/trace")
async def dialog_trace(request: Request, dialog_uuid: str):
    web = get_web_state(request)
    if not web.storage.get_dialog(dialog_uuid):
        raise HTTPException(status_code=404, detail="dialog 不存在")
    messages = web.storage.list_messages(dialog_uuid)
    last_assistant = None
    user_input = None
    for m in reversed(messages):
        if m["role"] == "assistant" and m.get("task_id"):
            last_assistant = m
            break
    if not last_assistant:
        return {"trace_run_id": None, "progress": [], "steps": []}

    progress = []
    if last_assistant.get("extra"):
        try:
            progress = json.loads(last_assistant["extra"])
        except (json.JSONDecodeError, TypeError):
            pass

    if last_assistant.get("trace_run_id"):
        steps = query_trace_steps(last_assistant["trace_run_id"])
        return {
            "trace_run_id": last_assistant["trace_run_id"],
            "progress": progress,
            "steps": steps.get("items", []),
        }
    for m in messages:
        if m["role"] == "user":
            user_input = m.get("content", "")
            break
    if not user_input:
        return {"trace_run_id": None, "progress": progress, "steps": []}
    run_id = find_trace_run_by_input(user_input, last_assistant.get("created_at"))
    if run_id:
        steps = query_trace_steps(run_id)
        return {"trace_run_id": run_id, "progress": progress, "steps": steps.get("items", [])}
    return {"trace_run_id": None, "progress": progress, "steps": []}


@router.get("/api/dialogs/{dialog_uuid}/runs")
async def dialog_runs(request: Request, dialog_uuid: str):
    """对话下全部 run 列表（多 run 选择器）。"""
    web = get_web_state(request)
    if not web.storage.get_dialog(dialog_uuid):
        raise HTTPException(status_code=404, detail="dialog 不存在")
    try:
        runs = web.storage.list_runs(dialog_uuid)
        return {"dialog_uuid": dialog_uuid, "runs": runs}
    except Exception as e:
        logger.error("list_runs 失败: %s", e)
        return {"dialog_uuid": dialog_uuid, "runs": []}


@router.get("/api/dialogs/{dialog_uuid}/attribution")
async def dialog_attribution(
    request: Request, dialog_uuid: str, run_id: str | None = None
):
    """某 run 的溯源聚合（概览/来源/工具/轨迹/关联/日志）。"""
    web = get_web_state(request)
    if not web.storage.get_dialog(dialog_uuid):
        raise HTTPException(status_code=404, detail="dialog 不存在")
    if not run_id:
        raise HTTPException(status_code=400, detail="缺少 run_id 参数")
    try:
        data = web.storage.get_attribution(dialog_uuid, run_id)
        data["dialog_uuid"] = dialog_uuid
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_attribution 失败: %s", e)
        raise HTTPException(status_code=500, detail="聚合失败")

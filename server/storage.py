"""Web 会话持久化。"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.app_paths import get_db_path


# 表结构 DDL 统一由 utils/db_schema 管理（见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md）。
# 创建/补列/迁移经 ensure_schema → registry["stock_radar"] 执行；此处不再内嵌 DDL。
_SQLITE_PARAM_CHUNK_SIZE = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_uuid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _truncate_query(inp) -> str:
    """把 runs.input（多为 JSON 包裹的 list/dict）压成 ≤80 字展示串。"""
    try:
        if isinstance(inp, str):
            try:
                v = json.loads(inp)
            except (json.JSONDecodeError, TypeError):
                v = inp
        else:
            v = inp
        if isinstance(v, (list, dict)):
            v = v[0] if isinstance(v, list) and v else json.dumps(v, ensure_ascii=False)
        return (v[:80] if isinstance(v, str) else str(v)[:80]) if v else ""
    except Exception:
        return ""


_MILESTONE_TYPES = {"classifier", "planner", "executor", "replanner", "unified_executor"}

_MILESTONE_LABELS = {
    "classifier": "意图分类",
    "planner": "生成执行计划",
    "executor": "执行步骤",
    "replanner": "重新规划",
    "unified_executor": "统一执行",
}


def _extract_symbol(extra) -> str | None:
    """从 step.extra 提取股票代码（symbol / code / stock_code，含嵌套 args）。"""
    if not isinstance(extra, dict):
        return None
    for k in ("symbol", "code", "stock_code"):
        if extra.get(k):
            return str(extra[k])
    args = extra.get("args") or extra.get("input") or {}
    if isinstance(args, dict):
        for k in ("symbol", "code", "stock_code"):
            if args.get(k):
                return str(args[k])
    return None


def _tool_step_summary(s: dict) -> dict:
    """从工具步骤的 input/output 提炼一行「证据」摘要（供来源条/溯源面板展示）。

    input 常见形如 {"query": "2026-08-20 A股主要指数表现 ..."}；
    output 常见形如 {"tables_count": 3, "total_rows": 12, "tables": [{sheet_name, rows}...]}
    或 {"status": "failed", "error": "..."}。统一截断防 payload 膨胀。
    """
    def _load(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return {"_raw": v[:200]}
        return v or {}

    inp = _load(s.get("input"))
    out = _load(s.get("output"))
    # 仅当 input 为带 query 字段的对象时才算查询语句（llm/agent 步骤的 input 是消息列表，无 query）
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
            elif out.get("error"):
                summary = f"失败：{str(out['error'])[:60]}"
    elif isinstance(out, dict) and out.get("_raw"):
        summary = str(out["_raw"])[:80]

    return {
        "step_id": s.get("id"),
        "query": str(query)[:140],
        "summary": str(summary)[:160],
        "status": s.get("status") or "unknown",
    }


def _aggregate_attribution(run_meta: dict, steps: list[dict], support_uuid) -> dict:
    """把 run + steps + support 消息聚合成「回答溯源」面板数据（ADR-002 v1 读时派生）。

    - sources：按 (domain, provider) 分组（来自 TOOL_DOMAIN_MAP）
    - tools：每个 step 一行
    - stages：milestone 级浓缩（classifier/planner/executor…）
    - correlation：v1 默认「一个 run → 其 assistant 消息」，全部 step 归因到 support_uuid
    - raw_log：由 steps 现推（INFO/WARN/ERROR 级别）
    """
    from server.constants import TOOL_DOMAIN_MAP

    domains: set[str] = set()
    sources: dict = {}       # (domain, provider) -> dict
    tools: list[dict] = []
    stages: list[dict] = []
    raw_log: list[dict] = []
    llm_count = 0

    for s in steps:
        st = s.get("step_type", "") or ""
        name = s.get("step_name", "") or ""
        status = s.get("status", "") or "unknown"
        dur = s.get("duration_ms")
        extra = s.get("extra") or {}
        started = s.get("started_at")
        if st == "llm":
            llm_count += 1

        meta = TOOL_DOMAIN_MAP.get(name, {})
        domain = meta.get("domain", "其他")
        provider = meta.get("provider", "—")
        display = meta.get("display_name", name)
        symbol = _extract_symbol(extra)

        tools.append({
            "step_id": s.get("id"),
            "step_name": name,
            "display_name": display,
            "domain": domain,
            "status": status,
            "duration_ms": dur,
            "symbol": symbol,
            "started_at": started,
            "support_message_uuid": support_uuid,
        })

        if domain != "其他":
            key = (domain, provider)
            if key not in sources:
                sources[key] = {
                    "id": f"s{len(sources) + 1}",
                    "domain": domain,
                    "provider": provider,
                    "label": f"{domain} · {provider}" if provider != "—" else domain,
                    "step_ids": [],
                    "steps": [],
                    "support_message_uuid": support_uuid,
                }
            sources[key]["step_ids"].append(str(s.get("id")))
            if len(sources[key]["steps"]) < 12:  # 防 payload 膨胀：每来源最多 12 条证据
                sources[key]["steps"].append(_tool_step_summary(s))
            domains.add(domain)

        if st in _MILESTONE_TYPES:
            stages.append({
                "stage": st,
                "label": _MILESTONE_LABELS.get(st, st),
                "status": status,
                "started_at": started,
                "duration_ms": dur,
                "support_message_uuid": support_uuid,
            })

        level = "ERROR" if status == "error" else ("WARN" if status == "running" else "INFO")
        text = f"{st} · {name} · {status}"
        if extra.get("error"):
            text += f" · {extra['error']}"
        # 步骤级详情（供「原始日志」完整展示：查询/结果/错误）
        step_summary = _tool_step_summary(s) if st in ("tool", "llm") else {"query": "", "summary": ""}
        err_detail = str(extra.get("error") or "")
        raw_log.append({
            "ts": started,
            "level": level,
            "run_id": run_meta.get("run_id"),
            "text": text,
            "step_id": s.get("id"),
            "step_type": st,
            "step_name": name,
            "status": status,
            "duration_ms": dur,
            "input": step_summary.get("query", ""),
            "output": step_summary.get("summary", ""),
            "error": err_detail[:300],
        })

    if not stages and steps:
        stages.append({
            "stage": "run",
            "label": "执行过程",
            "status": run_meta.get("status", "unknown"),
            "started_at": run_meta.get("started_at"),
            "duration_ms": run_meta.get("duration_ms", 0),
            "support_message_uuid": support_uuid,
        })

    overview = {
        "duration_ms": run_meta.get("duration_ms", 0),
        "data_source_count": len([d for d in domains if d != "其他"]),
        "tool_count": len(steps),
        "llm_call_count": llm_count,
        "status": run_meta.get("status", "unknown"),
        "support_message_count": 1 if support_uuid else 0,
    }

    correlation: dict = {}
    if support_uuid:
        correlation[support_uuid] = {
            "sources": [v["id"] for v in sources.values()],
            "tools": [str(t["step_id"]) for t in tools],
            "stages": [s["stage"] for s in stages],
        }

    return {
        "dialog_uuid": None,  # 由路由层填充
        "run": run_meta,
        "overview": overview,
        "sources": list(sources.values()),
        "tools": tools,
        "stages": stages,
        "correlation": correlation,
        "raw_log": raw_log,
    }


class WebStorage:
    """Web dialog 和 message 的 SQLite 存储。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or get_db_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA cache_size=-32768")
        self._lock = threading.RLock()
        self.ensure_schema()
        self.watchlist = WatchlistStorage(self._conn, self._lock)
        self.portfolio = PortfolioStorage(self._conn, self._lock)

    def close(self):
        """关闭底层连接（服务优雅退出时调用，避免 shutdown 阶段 ResourceWarning）。"""
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def _connect(self):
        return self._conn

    def ensure_schema(self):
        """对齐 stock_radar.db 全部表结构（DDL 单一事实源 utils/db_schema）。"""
        with self._lock:
            from utils.db_schema.migrator import ensure_schema as diff_schema
            from utils.db_schema.registry import ALL_DBS
            d = ALL_DBS["stock_radar"]
            diff_schema(self._conn, d.tables, d.special_migrations)
            self._conn.commit()

    def create_dialog(self, title: str = "新对话", mode: str = "react_stock") -> dict[str, Any]:
        now = utc_now()
        dialog_uuid = make_uuid("dlg")
        title = (title or "新对话").strip() or "新对话"
        with self._lock:
            conn = self._conn
            conn.execute(
                """
                INSERT INTO web_dialogs(dialog_uuid, title, current_mode, created_at, updated_at)
                VALUES(?,?,?,?,?)
                """,
                (dialog_uuid, title, mode, now, now),
            )
            conn.commit()
        return self.get_dialog(dialog_uuid)

    def get_dialog(self, dialog_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._conn
            row = conn.execute(
                "SELECT dialog_uuid,title,current_mode,title_auto,created_at,updated_at FROM web_dialogs WHERE dialog_uuid=?",
                (dialog_uuid,),
            ).fetchone()
        return dict(row) if row else None

    def list_dialogs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._conn
            rows = conn.execute(
                """
                SELECT dialog_uuid,title,current_mode,title_auto,created_at,updated_at
                FROM web_dialogs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_dialogs_by_created_range(self, start_at: str, end_at: str) -> list[dict[str, Any]]:
        """返回 created_at 落在 [start_at, end_at) 的对话。"""
        with self._lock:
            conn = self._conn
            rows = conn.execute(
                """
                SELECT dialog_uuid,title,current_mode,title_auto,created_at,updated_at
                FROM web_dialogs
                WHERE created_at>=? AND created_at<?
                ORDER BY created_at DESC
                """,
                (start_at, end_at),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_dialogs_since(self, start_at: str) -> int:
        """[start_at, ∞) 内创建的对话数（近 N 天活跃用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM web_dialogs WHERE created_at >= ?",
                (start_at,)).fetchone()
        return int(row[0] or 0)

    def count_messages_since(self, start_at: str) -> int:
        """[start_at, ∞) 内创建的消息数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM web_messages WHERE created_at >= ?",
                (start_at,)).fetchone()
        return int(row[0] or 0)

    def update_dialog(self, dialog_uuid: str, **fields) -> dict[str, Any] | None:
        allowed = {"title", "current_mode", "title_auto", "updated_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        updates["updated_at"] = updates.get("updated_at") or utc_now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [dialog_uuid]
        with self._lock:
            conn = self._conn
            conn.execute(
                f"UPDATE web_dialogs SET {assignments} WHERE dialog_uuid=?",
                values,
            )
            conn.commit()
        return self.get_dialog(dialog_uuid)

    def create_message(
        self,
        *,
        dialog_uuid: str,
        role: str,
        content: str,
        status: str,
        mode: str | None = None,
        task_id: str | None = None,
        log_uuid: str | None = None,
        log_file: str | None = None,
        trace_run_id: str | None = None,
        error: str | None = None,
        extra: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        message_uuid = make_uuid("msg")
        with self._lock:
            conn = self._conn
            conn.execute(
                """
                INSERT INTO web_messages(
                    message_uuid, dialog_uuid, role, content, status, mode, task_id,
                    log_uuid, log_file, trace_run_id, error, extra, created_at, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_uuid,
                    dialog_uuid,
                    role,
                    content,
                    status,
                    mode,
                    task_id,
                    log_uuid,
                    log_file,
                    trace_run_id,
                    error,
                    extra,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_message(message_uuid)

    def get_message(self, message_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._conn
            row = conn.execute(
                """
                SELECT message_uuid,dialog_uuid,role,content,status,mode,task_id,
                       log_uuid,log_file,trace_run_id,error,extra,created_at,updated_at
                FROM web_messages
                WHERE message_uuid=?
                """,
                (message_uuid,),
            ).fetchone()
        return dict(row) if row else None

    def list_messages(self, dialog_uuid: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._conn
            rows = conn.execute(
                """
                SELECT message_uuid,dialog_uuid,role,content,status,mode,task_id,
                       log_uuid,log_file,trace_run_id,error,extra,created_at,updated_at
                FROM web_messages
                WHERE dialog_uuid=?
                ORDER BY created_at ASC, id ASC
                """,
                (dialog_uuid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_trace_runs_for_dialogs(self, dialog_uuids: list[str]) -> dict[str, str]:
        if not dialog_uuids:
            return {}
        result: dict[str, str] = {}
        with self._lock:
            for i in range(0, len(dialog_uuids), _SQLITE_PARAM_CHUNK_SIZE):
                chunk = dialog_uuids[i:i + _SQLITE_PARAM_CHUNK_SIZE]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"""
                    SELECT dialog_uuid, trace_run_id
                    FROM (
                        SELECT dialog_uuid, trace_run_id,
                               ROW_NUMBER() OVER (PARTITION BY dialog_uuid ORDER BY id DESC) AS rn
                        FROM web_messages
                        WHERE dialog_uuid IN ({placeholders})
                          AND trace_run_id IS NOT NULL
                          AND trace_run_id != ''
                    )
                    WHERE rn=1
                    """,
                    chunk,
                ).fetchall()
                result.update({row["dialog_uuid"]: row["trace_run_id"] for row in rows})
        return result

    def list_runs(self, dialog_uuid: str) -> list[dict[str, Any]]:
        """返回对话下所有带 trace_run_id 的 run 轻量列表（多 run 选择器）。

        数据来源：web_messages（本对话的 trace_run_id + 对应 assistant message_uuid）
        + agent_trace.db runs/steps（状态/耗时/步骤数）。两步取数，
        不跨库 JOIN（两个独立 SQLite 文件）。
        """
        # 1) web_messages：本对话所有 trace_run_id（按出现顺序，去重保序）
        run_ids: list[str] = []
        support_map: dict[str, str] = {}  # run_id -> 最新 assistant message_uuid
        with self._lock:
            conn = self._conn
            rows = conn.execute(
                """
                SELECT trace_run_id, message_uuid, role
                FROM web_messages
                WHERE dialog_uuid=? AND trace_run_id IS NOT NULL AND trace_run_id != ''
                ORDER BY id ASC
                """,
                (dialog_uuid,),
            ).fetchall()
            for r in rows:
                rid = r["trace_run_id"]
                if rid not in run_ids:
                    run_ids.append(rid)
                if r["role"] == "assistant" and r["message_uuid"]:
                    support_map[rid] = r["message_uuid"]

        if not run_ids:
            return []

        # 2) agent_trace 月库（分库路由）：run 元信息 + steps 计数
        from utils.agent_trace.store import store
        runs: list[dict[str, Any]] = []
        run_rows = store.get_runs_by_ids(run_ids)
        if run_rows:
            run_by_id = {r["id"]: r for r in run_rows}
            for rid in run_ids:
                rr = run_by_id.get(rid)
                if not rr:
                    continue
                cnt = store.count_steps(rid)
                runs.append({
                    "run_id": rid,
                    "status": rr["status"] or "unknown",
                    "started_at": rr["created_at"],
                    "duration_ms": rr["duration_ms"] or 0,
                    "query": _truncate_query(rr["input"]),
                    "step_count": cnt,
                    "assistant_message_uuid": support_map.get(rid),
                })
        else:
            for rid in run_ids:
                runs.append({
                    "run_id": rid, "status": "unknown", "started_at": None,
                    "duration_ms": 0, "query": "", "step_count": 0,
                    "assistant_message_uuid": support_map.get(rid),
                })

        # 保持 web 侧出现顺序（最新 run 在末尾）
        runs.sort(key=lambda x: run_ids.index(x["run_id"]))
        return runs

    def get_attribution(self, dialog_uuid: str, run_id: str) -> dict[str, Any]:
        """聚合某 run 的溯源数据（概览/来源/工具/轨迹/关联/日志）。

        - run 与 steps 来自 agent_trace 月库（分库路由 store.conn_for_run）
        - support message 来自 web_messages（本对话中 trace_run_id == run_id 的 assistant 消息）
        - 关联（correlation）采用 ADR-002 v1「读时派生」：
          一个 run 默认对应其 assistant 消息（段级），全部 step 归因到该消息。
        """
        from utils.agent_trace.store import store
        tconn = store.conn_for_run(run_id)
        run_meta = {
            "run_id": run_id, "status": "unknown", "started_at": None,
            "duration_ms": 0, "query": "", "agent_name": "",
        }
        steps: list[dict[str, Any]] = []
        if tconn:
            rrow = tconn.execute(
                "SELECT id, agent_name, status, created_at, finished_at, duration_ms, input "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if rrow:
                run_meta = {
                    "run_id": rrow["id"],
                    "status": rrow["status"] or "unknown",
                    "started_at": rrow["created_at"],
                    "duration_ms": rrow["duration_ms"] or 0,
                    "query": _truncate_query(rrow["input"]),
                    "agent_name": rrow["agent_name"] or "",
                }
            srows = tconn.execute(
                "SELECT id, run_id, step_type, step_name, status, duration_ms, "
                "started_at, finished_at, extra, input, output "
                "FROM steps WHERE run_id=? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
            for s in srows:
                step = dict(s)
                extra = step.get("extra")
                if extra and isinstance(extra, str):
                    try:
                        step["extra"] = json.loads(extra)
                    except (json.JSONDecodeError, TypeError):
                        step["extra"] = {}
                else:
                    step["extra"] = extra or {}
                steps.append(step)

        # support message：本对话中 trace_run_id == run_id 的 assistant 消息（最新一条）
        support_uuid = None
        with self._lock:
            conn = self._conn
            mrow = conn.execute(
                "SELECT message_uuid FROM web_messages "
                "WHERE dialog_uuid=? AND trace_run_id=? AND role='assistant' "
                "ORDER BY id DESC LIMIT 1",
                (dialog_uuid, run_id),
            ).fetchone()
            if mrow:
                support_uuid = mrow["message_uuid"]

        return _aggregate_attribution(run_meta, steps, support_uuid)

    def correlate(self, q: str) -> dict[str, Any] | None:
        """按 dialog_uuid / web_dialog_uuid / task_id / trace_run_id 任一反查关联包。

        返回该消息行的关联键（dialog_uuid/task_id/log_file/trace_run_id），
        用于日志 × agent_trace 的关联检索面板（ADR-005 Phase 3）。

        搜索顺序：
          1) web_messages：对话消息行（含 trace_run_id / log_file / task_id），优先；
          2) dialog 表：agent 内部 dialog_uuid（无 trace_run_id，但可能有 log_file），
             作为回退，使直接传入 agent dialog_uuid 也能取到对话日志。
        """
        if not q:
            return None
        q = q.strip()
        with self._lock:
            conn = self._conn
            row = conn.execute(
                """
                SELECT dialog_uuid, task_id, log_file, trace_run_id, role, status
                FROM web_messages
                WHERE (dialog_uuid = ? OR task_id = ? OR trace_run_id = ?)
                  AND trace_run_id IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (q, q, q),
            ).fetchone()
            if row:
                return dict(row)
            # 回退：dialog 表（agent 内部 dialog_uuid）
            row = conn.execute(
                """
                SELECT dialog_uuid,
                       NULL AS task_id,
                       log_file,
                       NULL AS trace_run_id,
                       NULL AS role,
                       NULL AS status
                FROM dialog
                WHERE dialog_uuid = ?
                LIMIT 1
                """,
                (q,),
            ).fetchone()
        return dict(row) if row else None

    def update_message(self, message_uuid: str, **fields) -> dict[str, Any] | None:
        allowed = {
            "content",
            "status",
            "mode",
            "task_id",
            "log_uuid",
            "log_file",
            "trace_run_id",
            "error",
            "extra",
            "created_at",  # 完成时重排（澄清问答先落库时，最终回答需排在其后）
            "updated_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        updates["updated_at"] = updates.get("updated_at") or utc_now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [message_uuid]
        with self._lock:
            conn = self._conn
            conn.execute(
                f"UPDATE web_messages SET {assignments} WHERE message_uuid=?",
                values,
            )
            conn.commit()
        return self.get_message(message_uuid)

    def has_running_task(self, dialog_uuid: str) -> str | None:
        with self._lock:
            conn = self._conn
            row = conn.execute(
                """
                SELECT task_id
                FROM web_messages
                WHERE dialog_uuid=?
                  AND role='assistant'
                  AND status IN ('pending', 'streaming')
                  AND task_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (dialog_uuid,),
            ).fetchone()
        return row["task_id"] if row else None

    def mark_interrupted_messages(self) -> int:
        now = utc_now()
        with self._lock:
            conn = self._conn
            cur = conn.execute(
                """
                UPDATE web_messages
                SET status='failed',
                    error='server 已重启，任务未完成',
                    updated_at=?
                WHERE role='assistant'
                  AND status IN ('pending', 'streaming')
                """,
                (now,),
            )
            conn.commit()
        return cur.rowcount

    def delete_dialog(self, dialog_uuid: str) -> bool:
        with self._lock:
            conn = self._conn
            conn.execute("DELETE FROM web_messages WHERE dialog_uuid=?", (dialog_uuid,))
            conn.execute("DELETE FROM web_dialogs WHERE dialog_uuid=?", (dialog_uuid,))
            conn.commit()
        return True

    def touch_dialog_for_message(self, dialog_uuid: str, mode: str | None, content: str):
        dialog = self.get_dialog(dialog_uuid)
        if not dialog:
            return
        title = dialog["title"]
        # 首条消息先写提问截断占位标题（title_auto 保持 0，等待 classify 的 LLM 标题回填）
        if title == "新对话" and content.strip():
            title = content.strip().replace("\n", " ")[:30]
        self.update_dialog(
            dialog_uuid,
            title=title,
            current_mode=mode or dialog["current_mode"],
        )


class WatchlistStorage:
    """自选股 SQLite 存储。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def list_stocks(self, tag: str | None = None) -> list[dict[str, Any]]:
        cols = ("stock_code,stock_name,market,market_short,source,tags,"
                "price,change_pct,change_amt,high_price,low_price,"
                "turnover_rate,volume_ratio,volume,trading_amount,"
                "pe,pb,total_market_value,circulation_market_value,"
                "added_at,updated_at")
        with self._lock:
            if tag:
                rows = self._conn.execute(
                    f"SELECT {cols} FROM watchlist WHERE tags LIKE ? ORDER BY added_at DESC",
                    (f"%{tag}%",),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {cols} FROM watchlist ORDER BY added_at DESC",
                ).fetchall()
        return [dict(row) for row in rows]

    _INSERT_COLS = ("stock_code,stock_name,market,market_short,source,tags,"
                    "price,change_pct,change_amt,high_price,low_price,"
                    "turnover_rate,volume_ratio,volume,trading_amount,"
                    "pe,pb,total_market_value,circulation_market_value,"
                    "added_at,updated_at")

    def _row_values(self, s: dict, now: str) -> tuple:
        return (
            s.get("stock_code", ""), s.get("stock_name", ""),
            s.get("market", "cn"), s.get("market_short", ""),
            s.get("source", "local"), s.get("tags", "自选股"),
            s.get("price"), s.get("change_pct"), s.get("change_amt"),
            s.get("high_price"), s.get("low_price"),
            s.get("turnover_rate"), s.get("volume_ratio"),
            s.get("volume"), s.get("trading_amount"),
            s.get("pe"), s.get("pb"),
            s.get("total_market_value"), s.get("circulation_market_value"),
            now, now,
        )

    def _upsert_sql(self) -> str:
        return f"""
            INSERT INTO watchlist({self._INSERT_COLS})
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stock_code, market) DO UPDATE SET
                stock_name=excluded.stock_name,
                market_short=excluded.market_short,
                source=excluded.source,
                tags=excluded.tags,
                price=excluded.price,
                change_pct=excluded.change_pct,
                change_amt=excluded.change_amt,
                high_price=excluded.high_price,
                low_price=excluded.low_price,
                turnover_rate=excluded.turnover_rate,
                volume_ratio=excluded.volume_ratio,
                volume=excluded.volume,
                trading_amount=excluded.trading_amount,
                pe=excluded.pe,
                pb=excluded.pb,
                total_market_value=excluded.total_market_value,
                circulation_market_value=excluded.circulation_market_value,
                updated_at=excluded.updated_at
        """

    def add_stock(self, stock_code: str, stock_name: str = "", market: str = "cn", source: str = "local", tags: str = "自选股", **extra) -> dict[str, Any]:
        now = utc_now()
        s = {"stock_code": stock_code, "stock_name": stock_name, "market": market, "source": source, "tags": tags, **extra}
        with self._lock:
            self._conn.execute(self._upsert_sql(), self._row_values(s, now))
            self._conn.commit()
        return s

    def remove_stock(self, stock_code: str, market: str = "cn") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM watchlist WHERE stock_code=? AND market=?",
                (stock_code, market),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def update_quote(self, stock_code: str, market: str, quote: dict) -> bool:
        """仅更新行情字段，不改动 source/tags 等。"""
        now = utc_now()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE watchlist SET
                    stock_name=CASE WHEN ? != '' THEN ? ELSE stock_name END,
                    price=?, change_pct=?, change_amt=?,
                    high_price=?, low_price=?,
                    turnover_rate=?, volume_ratio=?,
                    pe=?, pb=?,
                    total_market_value=CASE WHEN ? != '' THEN ? ELSE total_market_value END,
                    circulation_market_value=CASE WHEN ? != '' THEN ? ELSE circulation_market_value END,
                    updated_at=?
                WHERE stock_code=? AND market=?""",
                (
                    quote.get("name", ""), quote.get("name", ""),
                    quote.get("price"), quote.get("change_pct"), quote.get("change_amt"),
                    quote.get("high"), quote.get("low"),
                    quote.get("turnover_rate"), quote.get("volume_ratio"),
                    quote.get("pe"), quote.get("pb"),
                    quote.get("total_mv", ""), quote.get("total_mv", ""),
                    quote.get("circ_mv", ""), quote.get("circ_mv", ""),
                    now, stock_code, market,
                ),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def bulk_replace(self, stocks: list[dict], source: str = "mx") -> int:
        now = utc_now()
        sql = self._upsert_sql()
        with self._lock:
            self._conn.execute("DELETE FROM watchlist WHERE source=?", (source,))
            for s in stocks:
                s["source"] = source
                self._conn.execute(sql, self._row_values(s, now))
            self._conn.commit()
        return len(stocks)


class PortfolioError(Exception):
    """持仓账本业务校验错误（卖出超持仓、重算不一致等）。"""


class PortfolioStorage:
    """持仓账本：流水为 source of truth，持仓表为事务内重算快照。

    成本口径（加权平均，用户确认 2026-09-06）：
    - 买入：avg_cost = (avg_cost×qty_old + price×qty_new + fee) / (qty_old + qty_new)
    - 卖出：realized_pnl += (price − avg_cost)×qty_sell − fee；avg_cost 不变
    - 清仓：qty=0 保留行（opened_at 可查历史），下次买入从 0 重新加权
    - 删流水：全量重算该股（从剩余流水按 trade_date,id 重放）
    """

    _TRADE_COLS = ("id,trade_uuid,stock_code,stock_name,market,side,qty,price,"
                   "trade_date,fee,source,signal_id,dialog_uuid,remark,created_at")

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    # -- 记账 / 更正 --------------------------------------------------------

    def add_trade(self, stock_code: str, side: str, qty: float, price: float,
                  trade_date: str, stock_name: str | None = None,
                  market: str = "cn", fee: float = 0.0, source: str = "manual",
                  signal_id: int | None = None, dialog_uuid: str | None = None,
                  remark: str | None = None) -> dict[str, Any]:
        """记一笔交易：事务内插流水 + 全量重算持仓快照。"""
        if side not in ("buy", "sell"):
            raise PortfolioError(f"无效交易方向: {side}")
        if qty <= 0 or price <= 0:
            raise PortfolioError("数量和价格必须为正数")
        trade_uuid = make_uuid("ptr")
        now = utc_now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if side == "sell":
                    self._assert_sellable(stock_code, market, qty)
                self._conn.execute(
                    "INSERT INTO portfolio_trade(trade_uuid,stock_code,stock_name,market,"
                    "side,qty,price,trade_date,fee,source,signal_id,dialog_uuid,remark,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trade_uuid, stock_code, stock_name, market, side, qty, price,
                     trade_date, fee, source, signal_id, dialog_uuid, remark, now),
                )
                self._recompute_position(stock_code, market)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"trade_uuid": trade_uuid, "stock_code": stock_code, "side": side,
                "qty": qty, "price": price, "trade_date": trade_date}

    def remove_trade(self, trade_uuid: str) -> bool:
        """删流水并全量重算该股持仓（记错单更正）。

        重算失败（如删除后出现卖出超持仓）则整体回滚、拒绝删除。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT stock_code,market FROM portfolio_trade WHERE trade_uuid=?",
                (trade_uuid,)).fetchone()
            if row is None:
                return False
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM portfolio_trade WHERE trade_uuid=?", (trade_uuid,))
                self._recompute_position(row["stock_code"], row["market"])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return True

    def _assert_sellable(self, stock_code: str, market: str, qty_sell: float) -> None:
        row = self._conn.execute(
            "SELECT qty FROM portfolio_position WHERE stock_code=? AND market=?",
            (stock_code, market)).fetchone()
        held = row["qty"] if row else 0.0
        if held < qty_sell:
            raise PortfolioError(f"持仓不足：当前 {held} 股，卖出 {qty_sell} 股被拒")

    # -- 持仓重算（source of truth → 快照） -----------------------------------

    def _recompute_position(self, stock_code: str, market: str) -> None:
        """按 (trade_date, id) 重放该股全部流水，UPSERT 持仓快照。

        任何一步卖出超出累计买入量 → raise（调用方事务回滚）。
        """
        trades = self._conn.execute(
            f"SELECT {self._TRADE_COLS} FROM portfolio_trade"
            " WHERE stock_code=? AND market=? ORDER BY trade_date, id",
            (stock_code, market)).fetchall()
        if not trades:
            self._conn.execute(
                "DELETE FROM portfolio_position WHERE stock_code=? AND market=?",
                (stock_code, market))
            return

        qty, avg_cost, realized = 0.0, 0.0, 0.0
        stock_name, opened_at = None, None
        for t in trades:
            stock_name = stock_name or t["stock_name"]
            opened_at = opened_at or t["trade_date"]
            if t["side"] == "buy":
                total = avg_cost * qty + t["price"] * t["qty"] + (t["fee"] or 0)
                qty += t["qty"]
                avg_cost = total / qty
            else:
                if qty < t["qty"]:
                    raise PortfolioError(
                        f"持仓不足：{t['trade_date']} 卖出 {t['qty']} 股超出可用量 {qty}")
                realized += (t["price"] - avg_cost) * t["qty"] - (t["fee"] or 0)
                qty -= t["qty"]

        self._conn.execute(
            """INSERT INTO portfolio_position
               (stock_code,market,stock_name,qty,avg_cost,realized_pnl,opened_at,updated_at,
                source,etf_flag,today_pnl)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(stock_code, market) DO UPDATE SET
                   stock_name=excluded.stock_name, qty=excluded.qty,
                   avg_cost=excluded.avg_cost, realized_pnl=excluded.realized_pnl,
                   opened_at=excluded.opened_at, updated_at=excluded.updated_at,
                   source=excluded.source,
                   etf_flag=CASE
                       WHEN portfolio_position.etf_flag = 1
                       THEN portfolio_position.etf_flag
                       ELSE excluded.etf_flag END,
                   today_pnl=CASE
                       WHEN excluded.today_pnl IS NULL
                       THEN portfolio_position.today_pnl
                       ELSE excluded.today_pnl END""",
            (stock_code, market, stock_name, qty, avg_cost, realized,
             opened_at, utc_now(),
             # 手动流水重算：非东财行恢复 manual 标记；东财行保留标记
             # （§3.7 券商为事实源；用户在东财行记交易 → 下次同步 diff 列为 updated）
             self._row_source(stock_code, market), 0, None))

    def _row_source(self, stock_code: str, market: str) -> str:
        """读取既有行来源（无行/查询失败 → 'manual'）。"""
        try:
            row = self._conn.execute(
                "SELECT source FROM portfolio_position WHERE stock_code=? AND market=?",
                (stock_code, market)).fetchone()
            return (row["source"] or "manual") if row else "manual"
        except Exception:
            return "manual"

    # -- 查询 ----------------------------------------------------------------

    def list_trades(self, stock_code: str | None = None, market: str = "cn",
                    page: int = 1, size: int = 50) -> dict[str, Any]:
        """流水分页查询（时间倒序），可按代码过滤。"""
        where, params = "", []
        if stock_code:
            where = " WHERE stock_code=?"
            params.append(stock_code)
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM portfolio_trade{where}", params).fetchone()[0]
        offset = max(page - 1, 0) * size
        rows = self._conn.execute(
            f"SELECT {self._TRADE_COLS} FROM portfolio_trade{where}"
            f" ORDER BY trade_date DESC, id DESC LIMIT ? OFFSET ?",
            params + [size, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total,
                "page": page, "size": size}

    def list_positions(self, market: str | None = None) -> list[dict[str, Any]]:
        """持仓快照直读（含 qty=0 的清仓历史行）。"""
        sql = ("SELECT stock_code,market,stock_name,qty,avg_cost,realized_pnl,"
               "today_pnl,opened_at,updated_at,source,avail_qty,etf_flag"
               " FROM portfolio_position")
        params: list = []
        if market:
            sql += " WHERE market=?"
            params.append(market)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def position_summary(self, latest_quotes: dict | None = None) -> dict[str, Any]:
        """持仓汇总：成本侧必算；行情缺失时市值/浮盈为 None。

        Args:
            latest_quotes: {code: {"price": 现价}}，仅统计 qty>0 的持仓。
        """
        positions = [p for p in self.list_positions() if p["qty"] > 0]
        quotes = latest_quotes or {}
        total_cost = sum(p["avg_cost"] * p["qty"] for p in positions)
        market_values, float_pnls = [], []
        for p in positions:
            q = quotes.get(p["stock_code"])
            if q and q.get("price"):
                market_values.append(q["price"] * p["qty"])
                float_pnls.append((q["price"] - p["avg_cost"]) * p["qty"])
        realized = sum(p["realized_pnl"] for p in self.list_positions())
        return {
            "position_count": len(positions),
            "total_cost": total_cost,
            "total_market_value": sum(market_values) if market_values else None,
            "total_float_pnl": sum(float_pnls) if float_pnls else None,
            "total_realized_pnl": realized,
        }

    # -- 东财同步（§3.7 快照对齐：覆盖式幂等，券商为事实源） -------------------

    def import_eastmoney_snapshot(self, positions: list[dict],
                                  removed_codes: list[str] | None = None) -> dict:
        """东财快照覆盖式落库（幂等）。

        - 快照行 UPSERT 为 source='eastmoney'（qty/avg_cost/avail_qty/etf_flag
          以券商为准，§3.7）；unchanged 行数值相同时 SQLite 仍会更新 updated_at，
          故由调用方传 unchanged 数量以便汇报"零差异零写入"语义。
        - removed_codes：原 eastmoney 行从快照消失 → qty=0 标记清仓（保留历史，
          设计决策点 2 已确认）。
        Returns:
            {"added": n, "updated": n, "removed": n, "unchanged": n}
        """
        now = utc_now()
        removed_codes = removed_codes or []
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for p in positions:
                    self._conn.execute(
                        """INSERT INTO portfolio_position
                           (stock_code,market,stock_name,qty,avg_cost,realized_pnl,
                            opened_at,updated_at,source,avail_qty,etf_flag,today_pnl)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(stock_code, market) DO UPDATE SET
                               stock_name=excluded.stock_name,
                               qty=excluded.qty,
                               avg_cost=excluded.avg_cost,
                               opened_at=CASE
                                   WHEN portfolio_position.opened_at IS NULL
                                    OR portfolio_position.opened_at = ''
                                   THEN excluded.opened_at
                                   ELSE portfolio_position.opened_at END,
                               updated_at=excluded.updated_at,
                               source='eastmoney',
                               avail_qty=excluded.avail_qty,
                               etf_flag=excluded.etf_flag,
                               today_pnl=excluded.today_pnl""",
                        (p["stock_code"], p.get("market", "cn"),
                         p.get("stock_name") or "", p["qty"], p["avg_cost"],
                         0, now, now, "eastmoney",
                         p.get("avail_qty"), 1 if p.get("is_etf") else 0,
                         p.get("today_pnl")))
                for code in removed_codes:
                    self._conn.execute(
                        """UPDATE portfolio_position
                           SET qty=0, avail_qty=0, updated_at=?
                           WHERE stock_code=? AND market='cn' AND source='eastmoney'""",
                        (now, code))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "added": len([p for p in positions if not p.get("_was_existing")]),
            "updated": len([p for p in positions if p.get("_was_existing")]),
            "removed": len(removed_codes),
            "unchanged": len([p for p in positions if p.get("_was_unchanged")]),
        }

    def eastmoney_sync_meta(self) -> dict[str, Any]:
        """上次同步信息（GET /eastmoney/session 用，无凭据）。"""
        with self._lock:
            em_rows = self._conn.execute(
                "SELECT COUNT(*) AS c, MAX(updated_at) AS t FROM portfolio_position"
                " WHERE source='eastmoney'").fetchone()
            etf_rows = self._conn.execute(
                "SELECT COUNT(*) AS c FROM portfolio_position"
                " WHERE source='eastmoney' AND etf_flag=1 AND qty>0").fetchone()
        source_counts: dict[str, int] = {}
        for row in self._conn.execute(
                "SELECT source, COUNT(*) AS c FROM portfolio_position GROUP BY source"):
            source_counts[row["source"] or "manual"] = row["c"]
        return {
            "last_synced_at": em_rows["t"] if em_rows else None,
            "eastmoney_count": em_rows["c"] if em_rows else 0,
            "eastmoney_etf_count": etf_rows["c"] if etf_rows else 0,
            "source_counts": source_counts,
        }

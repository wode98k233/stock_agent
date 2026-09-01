import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from utils.agent_trace.models import _DDL, _json, _now

logger = logging.getLogger(__name__)


class DBAdapter:
    def init_schema(self):
        raise NotImplementedError

    def insert_run(self, run: dict) -> str:
        raise NotImplementedError

    def update_run(self, run_id: str, **fields):
        raise NotImplementedError

    def insert_step(self, step: dict) -> int:
        raise NotImplementedError

    def update_step(self, step_id: int, **fields):
        raise NotImplementedError

    def insert_messages(self, messages: list):
        raise NotImplementedError

    def get_run(self, run_id: str) -> Optional[dict]:
        raise NotImplementedError

    def get_steps(self, run_id: str) -> list:
        raise NotImplementedError

    def get_runs(self, limit: int = 100, status: str = "") -> list:
        raise NotImplementedError

    def delete_run(self, run_id: str):
        raise NotImplementedError

    def get_steps_with_messages(self, run_id: str) -> list:
        raise NotImplementedError

    def close(self):
        pass


class SQLiteAdapter(DBAdapter):
    def __init__(self, db_path: str = "agent_trace.db"):
        self._db = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._ddl_ran: bool = False
        if str(self._db) != ":memory:":
            from utils.agent_trace.models import _ensure_db
            _ensure_db(self._db)
            self._ddl_ran = True

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            if not self._ddl_ran:
                self._conn.executescript(_DDL)
                self._conn.commit()
                self._ddl_ran = True
        return self._conn

    def init_schema(self):
        self._get_conn()

    def insert_run(self, run: dict) -> str:
        run_id = run["id"]
        agent_name = run.get("agent_name", "unknown")
        input_data = run.get("input", "")
        status = run.get("status", "running")
        created_at = run.get("created_at") or _now()
        with self._lock:
            c = self._get_conn()
            c.execute(
                "INSERT OR IGNORE INTO runs(id,agent_name,input,status,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, agent_name, _json(input_data), status, created_at),
            )
            c.commit()
        return run_id

    def update_run(self, run_id: str, **fields):
        if not fields:
            return
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(run_id)
        with self._lock:
            c = self._get_conn()
            c.execute(
                f"UPDATE runs SET {','.join(sets)} WHERE id=?", vals
            )
            c.commit()

    def insert_step(self, step: dict) -> int:
        with self._lock:
            c = self._get_conn()
            cur = c.execute(
                "INSERT INTO steps(run_id,event_run_id,parent_run_id,"
                "step_type,step_name,input,status,started_at,extra) "
                "VALUES(?,?,?,?,?,?,'running',?,?)",
                (
                    step["run_id"],
                    step["event_run_id"],
                    step.get("parent_run_id"),
                    step["step_type"],
                    step.get("step_name", ""),
                    _json(step.get("input")),
                    step.get("started_at") or _now(),
                    json.dumps(step["extra"], ensure_ascii=False) if step.get("extra") else None,
                ),
            )
            sid = cur.lastrowid
            c.commit()
            return sid

    def update_step(self, step_id: int, **fields):
        if not fields:
            return
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(step_id)
        with self._lock:
            c = self._get_conn()
            c.execute(
                f"UPDATE steps SET {','.join(sets)} WHERE id=?", vals
            )
            c.commit()

    def insert_messages(self, messages: list):
        if not messages:
            return
        with self._lock:
            c = self._get_conn()
            for m in messages:
                c.execute(
                    "INSERT INTO messages(step_id,seq,role,content,tool_calls,tool_call_id,reasoning) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        m["step_id"],
                        m["seq"],
                        m["role"],
                        m.get("content"),
                        m.get("tool_calls"),
                        m.get("tool_call_id"),
                        m.get("reasoning"),
                    ),
                )
            c.commit()

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            c = self._get_conn()
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def get_steps(self, run_id: str) -> list:
        with self._lock:
            c = self._get_conn()
            rows = c.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_runs(self, limit: int = 100, status: str = "") -> list:
        where = ""
        params: list = []
        if status:
            where = "WHERE status=?"
            params.append(status)
        with self._lock:
            c = self._get_conn()
            rows = c.execute(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_run(self, run_id: str):
        with self._lock:
            c = self._get_conn()
            step_ids = [
                r[0]
                for r in c.execute(
                    "SELECT id FROM steps WHERE run_id=?", (run_id,)
                ).fetchall()
            ]
            if step_ids:
                placeholders = ",".join("?" * len(step_ids))
                c.execute(
                    f"DELETE FROM messages WHERE step_id IN ({placeholders})",
                    step_ids,
                )
            c.execute("DELETE FROM steps WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM runs WHERE id=?", (run_id,))
            c.commit()

    def get_steps_with_messages(self, run_id: str) -> list:
        with self._lock:
            c = self._get_conn()
            steps = c.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            if not steps:
                return []
            step_ids = [s["id"] for s in steps]
            placeholders = ",".join("?" * len(step_ids))
            msg_rows = c.execute(
                f"SELECT * FROM messages WHERE step_id IN ({placeholders}) "
                "ORDER BY step_id, seq",
                step_ids,
            ).fetchall()
            msg_by_step: dict = {}
            for m in msg_rows:
                msg_by_step.setdefault(m["step_id"], []).append(dict(m))
            result = []
            for s in steps:
                d = dict(s)
                d["messages"] = msg_by_step.get(s["id"], [])
                result.append(d)
            return result

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ------------------------------------------------------------
# v2 封装 — 按 run_id 反查完整决策链
# ------------------------------------------------------------
def get_decision_chain(run_id: str) -> dict:
    """按 trace run_id 反查完整决策链：工具调用 + LLM 调用 + 推理路径。

    供 memory/provenance.py 的 ProvenanceQuerier 使用。
    返回结构化 dict 供前端展示"这条结论是怎么推断出来的"。
    """
    import os as _os
    db_path = _os.environ.get(
        "AGENT_TRACE_DB",
        _os.path.join(_os.path.dirname(__file__), "..", "..", "agent_trace.db"),
    )
    db_path = _os.path.abspath(db_path)
    if not _os.path.exists(db_path):
        return None

    adapter = SQLiteAdapter(db_path)
    try:
        adapter.init_schema()
        steps = adapter.get_steps_with_messages(run_id)
        if not steps:
            return None

        tool_calls = []
        llm_calls = []
        for s in steps:
            step_type = s.get("step_type", "")
            step_name = s.get("step_name", "")
            messages = s.get("messages", [])

            if step_type == "tool":
                tool_calls.append({
                    "name": step_name,
                    "input": s.get("input", ""),
                    "status": s.get("status", ""),
                })
            elif step_type in ("llm", "chat"):
                for m in messages:
                    role = m.get("role", "")
                    content = m.get("content", "")
                    if content:
                        llm_calls.append({
                            "step": step_name,
                            "role": role,
                            "content": content[:500],
                            "tool_calls": m.get("tool_calls"),
                        })

        # 构造推理路径（串联 LLM 调用摘要）
        reasoning_parts = []
        for llm_call in llm_calls:
            content = llm_call.get("content", "")
            if content and len(content) > 20:
                reasoning_parts.append(content[:200])
        reasoning_path = " → ".join(reasoning_parts)

        return {
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "reasoning_path": reasoning_path[:2000],
        }
    except Exception:
        return None
    finally:
        adapter.close()

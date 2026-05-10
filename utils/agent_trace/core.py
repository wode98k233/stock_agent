import json
import threading
import time
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from utils.agent_trace.models import (
    _DDL,
    _GRAPH_NODE_LABELS,
    _connect,
    _ensure_db,
    _guard,
    _identify_graph_node,
    _json,
    _msg_info,
    _now,
    _pick_name,
)


class TraceRecorder(BaseCallbackHandler):
    name = "TraceRecorder"
    raise_error = False

    def __init__(self, agent_name: str = "unknown", db_path: str = "agent_trace.db"):
        super().__init__()
        self.agent_name = agent_name
        self._db = Path(db_path)
        self._ddl_ran: bool = False
        if str(self._db) != ":memory:":
            _ensure_db(self._db)
            self._ddl_ran = True
        self._root: Optional[str] = None
        self._run_created: bool = False
        self._sid: Dict[str, int] = {}
        self._t: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self):
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

    def _tick(self, rid: str):
        self._t[rid] = time.perf_counter()

    def _ms(self, rid: str) -> Optional[float]:
        t = self._t.get(rid)
        return round((time.perf_counter() - t) * 1000, 2) if t else None

    def _create_step(self, run_id, parent_run_id, step_type, step_name, input_data, extra=None):
        rid = str(run_id)
        prid = str(parent_run_id) if parent_run_id else None
        self._tick(rid)

        with self._lock:
            c = self._get_conn()
            cur = c.execute(
                "INSERT INTO steps(run_id,event_run_id,parent_run_id,"
                "step_type,step_name,input,status,started_at,extra) "
                "VALUES(?,?,?,?,?,?,'running',?,?)",
                (self._root, rid, prid, step_type, step_name, _json(input_data),
                 _now(), json.dumps(extra, ensure_ascii=False) if extra else None),
            )
            sid = cur.lastrowid
            self._sid[rid] = sid
            c.commit()
            return sid

    def _finish_step(self, run_id, status="success", output=None, error=""):
        rid = str(run_id)
        ms = self._ms(rid)
        sid = self._sid.get(rid)
        if not sid:
            return
        with self._lock:
            c = self._get_conn()
            if status == "error":
                c.execute(
                    "UPDATE steps SET error=?,status='error',"
                    "finished_at=?,duration_ms=? WHERE id=?",
                    (str(error), _now(), ms, sid),
                )
            else:
                c.execute(
                    "UPDATE steps SET output=?,status='success',"
                    "finished_at=?,duration_ms=? WHERE id=?",
                    (_json(output), _now(), ms, sid),
                )
            c.commit()

    def start_run(self, task_input="") -> str:
        self._root = str(uuid4())
        self._run_created = True
        with self._lock:
            c = self._get_conn()
            c.execute(
                "INSERT OR IGNORE INTO runs(id,agent_name,input,status,created_at) "
                "VALUES(?,?,?,'running',?)",
                (self._root, self.agent_name, _json(task_input), _now()),
            )
            c.commit()
        return self._root

    def end_run(self, output="", status="success", error=""):
        if not self._root:
            return
        with self._lock:
            c = self._get_conn()
            c.execute(
                "UPDATE runs SET output=?,status=?,error=?,finished_at=? "
                "WHERE id=?",
                (_json(output), status, error, _now(), self._root),
            )
            c.commit()

    def close(self):
        self._t.clear()
        self._sid.clear()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @_guard
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
        node_type, display_name, label, is_noise = _identify_graph_node(
            serialized, kw, parent_run_id=parent_run_id
        )

        if self._root is None:
            self._root = str(run_id)

        if parent_run_id is None and not self._run_created:
            with self._lock:
                c = self._get_conn()
                c.execute(
                    "INSERT OR IGNORE INTO runs(id,agent_name,input,status,created_at) "
                    "VALUES(?,?,?,'running',?)",
                    (str(run_id), self.agent_name, _json(inputs), _now()),
                )
                c.commit()

        extra = {}
        if label:
            extra["label"] = label
        if is_noise:
            extra["_noise"] = True

        self._create_step(run_id, parent_run_id, node_type, display_name, inputs, extra)

    @_guard
    def on_chain_end(self, outputs, *, run_id, **kw):
        self._finish_step(run_id, output=outputs)

    @_guard
    def on_chain_error(self, error, *, run_id, **kw):
        self._finish_step(run_id, status="error", error=error)

    def _llm_start(self, serialized, run_id, parent_run_id, raw_input, **kw):
        rid = str(run_id)
        prid = str(parent_run_id) if parent_run_id else None
        self._tick(rid)

        if self._root is None:
            self._root = rid

        name = _pick_name(serialized)
        params = kw.get("invocation_params", {})
        extra = {k: params[k] for k in ("model_name", "temperature", "max_tokens")
                 if k in params}

        metadata = kw.get("metadata", {})
        langgraph_node = metadata.get("langgraph_node", "")
        if langgraph_node:
            extra["label"] = _GRAPH_NODE_LABELS.get(langgraph_node, langgraph_node)
            extra["node"] = langgraph_node

        with self._lock:
            c = self._get_conn()
            cur = c.execute(
                "INSERT INTO steps(run_id,event_run_id,parent_run_id,"
                "step_type,step_name,input,status,started_at,extra) "
                "VALUES(?,?,?,'llm',?,?,'running',?,?)",
                (self._root, rid, prid, name, _json(raw_input),
                 _now(), json.dumps(extra, ensure_ascii=False) if extra else None),
            )
            sid = cur.lastrowid
            self._sid[rid] = sid

            seq = 0
            for item in (raw_input if isinstance(raw_input, list) else []):
                msgs = item if isinstance(item, list) else [item]
                for m in msgs:
                    if hasattr(m, "type"):
                        d = _msg_info(m)
                        c.execute(
                            "INSERT INTO messages"
                            "(step_id,seq,role,content,tool_calls,tool_call_id) "
                            "VALUES(?,?,?,?,?,?)",
                            (sid, seq, d["role"], d["content"],
                             d["tool_calls"], d["tool_call_id"]),
                        )
                        seq += 1
            c.commit()

    @_guard
    def on_llm_start(self, serialized, prompts, *, run_id,
                     parent_run_id=None, **kw):
        self._llm_start(serialized, run_id, parent_run_id, prompts, **kw)

    @_guard
    def on_chat_model_start(self, serialized, messages, *, run_id,
                            parent_run_id=None, **kw):
        self._llm_start(serialized, run_id, parent_run_id, messages, **kw)

    @_guard
    def on_llm_end(self, response: LLMResult, *, run_id, **kw):
        rid = str(run_id)
        ms = self._ms(rid)
        sid = self._sid.get(rid)
        if not sid:
            return

        with self._lock:
            c = self._get_conn()
            row = c.execute(
                "SELECT COALESCE(MAX(seq),-1) FROM messages WHERE step_id=?",
                (sid,),
            ).fetchone()
            seq = row[0] + 1

            parts = []
            for gens in response.generations:
                for g in gens:
                    if hasattr(g, "message"):
                        d = _msg_info(g.message)
                        parts.append(d)
                        c.execute(
                            "INSERT INTO messages"
                            "(step_id,seq,role,content,tool_calls,tool_call_id) "
                            "VALUES(?,?,?,?,?,?)",
                            (sid, seq, d["role"], d["content"],
                             d["tool_calls"], d["tool_call_id"]),
                        )
                        seq += 1
                    else:
                        parts.append({"role": "assistant", "content": g.text})

            usage = (response.llm_output or {}).get("token_usage", {})
            existing_extra = {}
            try:
                row = c.execute("SELECT extra FROM steps WHERE id=?", (sid,)).fetchone()
                if row and row["extra"]:
                    existing_extra = json.loads(row["extra"])
            except Exception:
                pass
            existing_extra["token_usage"] = usage
            extra = json.dumps(existing_extra, default=str)

            c.execute(
                "UPDATE steps SET output=?,status='success',"
                "finished_at=?,duration_ms=?,extra=? WHERE id=?",
                (_json(parts), _now(), ms, extra, sid),
            )
            c.commit()

    @_guard
    def on_llm_error(self, error, *, run_id, **kw):
        self._finish_step(run_id, status="error", error=error)

    @_guard
    def on_tool_start(self, serialized, input_str, *, run_id,
                      parent_run_id=None, inputs=None, **kw):
        rid = str(run_id)
        prid = str(parent_run_id) if parent_run_id else None

        if self._root is None:
            self._root = rid

        name = (serialized or {}).get("name", "unknown_tool")
        tool_input = inputs if inputs is not None else input_str

        metadata = kw.get("metadata", {})
        langgraph_node = metadata.get("langgraph_node", "")
        extra = {}
        if langgraph_node:
            extra["label"] = _GRAPH_NODE_LABELS.get(langgraph_node, langgraph_node)
            extra["node"] = langgraph_node

        self._create_step(run_id, parent_run_id, "tool", name, tool_input, extra)

    @_guard
    def on_tool_end(self, output, *, run_id, **kw):
        output_str = str(output) if not isinstance(output, str) else output
        self._finish_step(run_id, output=output_str)

    @_guard
    def on_tool_error(self, error, *, run_id, **kw):
        self._finish_step(run_id, status="error", error=error)

    @_guard
    def on_retriever_start(self, serialized, query, *, run_id,
                           parent_run_id=None, **kw):
        rid = str(run_id)
        prid = str(parent_run_id) if parent_run_id else None

        if self._root is None:
            self._root = rid

        name = _pick_name(serialized or {})
        self._create_step(run_id, parent_run_id, "retriever", name, query)

    @_guard
    def on_retriever_end(self, documents, *, run_id, **kw):
        docs = [
            {"page_content": d.page_content, "metadata": d.metadata}
            for d in (documents or [])
        ]
        self._finish_step(run_id, output=docs)

    @_guard
    def on_retriever_error(self, error, *, run_id, **kw):
        self._finish_step(run_id, status="error", error=error)

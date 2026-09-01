import json
import logging
import threading
import time
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

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

# 全局 ContextVar：AgentRunContext 设置后，所有 LLM 调用自动 trace
_active_recorder: ContextVar[Optional["TraceRecorder"]] = ContextVar(
    "active_trace_recorder", default=None
)


class TraceRecorder(BaseCallbackHandler):
    name = "TraceRecorder"
    raise_error = False

    def __init__(self, agent_name: str = "unknown", db_path: str = "agent_trace.db",
                 db_adapter=None):
        super().__init__()
        self.agent_name = agent_name
        self._root: Optional[str] = None
        self._run_created: bool = False
        self._sid: Dict[str, int] = {}
        self._t: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._recent_llm_events: Dict[str, float] = {}
        self._dedup_window_ms = 100

        self._db = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._db_adapter = db_adapter

        if db_adapter is not None:
            self._ddl_ran = True
        else:
            self._ddl_ran: bool = False
            if str(self._db) != ":memory:":
                _ensure_db(self._db)
                self._ddl_ran = True

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
        self._run_t0 = time.perf_counter()
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
        duration_ms = None
        if hasattr(self, '_run_t0') and self._run_t0:
            duration_ms = round((time.perf_counter() - self._run_t0) * 1000, 2)
        with self._lock:
            c = self._get_conn()
            c.execute(
                "UPDATE runs SET output=?,status=?,error=?,finished_at=?,duration_ms=? "
                "WHERE id=?",
                (_json(output), status, error, _now(), duration_ms, self._root),
            )
            c.commit()

    def get_run_totals(self, run_id: str | None = None) -> dict:
        """从 SQLite steps 表聚合当前 run 的 LLM token 与调用次数。"""
        rid = run_id or self._root
        if not rid:
            return {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "cached_tokens": 0, "cache_hit_ratio": 0.0}
        try:
            from utils.token_usage import normalize_token_usage_dict
            with self._lock:
                c = self._get_conn()
                rows = c.execute(
                    "SELECT extra FROM steps WHERE run_id=? AND step_type='llm'",
                    (rid,),
                ).fetchall()
            llm_calls = 0
            input_tokens = 0
            output_tokens = 0
            total_tokens = 0
            cached_tokens = 0
            for row in rows:
                extra_str = row["extra"] if hasattr(row, "__getitem__") else row[0]
                if not extra_str:
                    continue
                try:
                    extra = json.loads(extra_str)
                    tu = normalize_token_usage_dict(extra.get("token_usage"))
                    llm_calls += 1
                    input_tokens += tu["input_tokens"]
                    output_tokens += tu["output_tokens"]
                    total_tokens += tu["total_tokens"]
                    cached_tokens += tu.get("cached_tokens", 0)
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
            hit_ratio = round(cached_tokens / input_tokens, 4) if input_tokens > 0 else 0.0
            return {
                "llm_calls": llm_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cached_tokens": cached_tokens,
                "cache_hit_ratio": hit_ratio,
            }
        except Exception as e:
            logger.warning("[TraceRecorder] get_run_totals error: %s", e)
            return {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "cached_tokens": 0, "cache_hit_ratio": 0.0}

    def close(self):
        self._t.clear()
        self._sid.clear()
        self._recent_llm_events.clear()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if hasattr(self, '_db_adapter') and self._db_adapter is not None:
            self._db_adapter.close()

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

        # 短窗口幂等防御
        dedup_key = f"{rid}:llm_start"
        now = time.perf_counter()
        last = self._recent_llm_events.get(dedup_key)
        if last is not None and (now - last) * 1000 < self._dedup_window_ms:
            logger.warning("[TraceRecorder] 重复 llm_start 被跳过: run_id=%s", rid)
            return
        self._recent_llm_events[dedup_key] = now
        # 清理过期条目
        if len(self._recent_llm_events) > 1000:
            cutoff = now - 0.5
            self._recent_llm_events = {
                k: v for k, v in self._recent_llm_events.items() if v > cutoff
            }

        self._tick(rid)

        if self._root is None:
            self._root = rid

        name = _pick_name(serialized)
        params = kw.get("invocation_params", {})
        extra = {k: params[k] for k in ("model_name", "temperature", "max_tokens")
                 if k in params}

        metadata = kw.get("metadata", {})
        node = metadata.get("node", "")
        langgraph_node = metadata.get("langgraph_node", "")
        effective_node = node or langgraph_node
        if effective_node:
            extra["label"] = _GRAPH_NODE_LABELS.get(effective_node, effective_node)
            extra["node"] = effective_node
        pdor_context = metadata.get("pdor_context", "")
        if pdor_context:
            extra["pdor_context"] = pdor_context

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
                            "(step_id,seq,role,content,tool_calls,tool_call_id,reasoning) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (sid, seq, d["role"], d["content"],
                             d["tool_calls"], d["tool_call_id"], d.get("reasoning")),
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

        # 短窗口幂等防御
        dedup_key = f"{rid}:llm_end"
        now = time.perf_counter()
        last = self._recent_llm_events.get(dedup_key)
        if last is not None and (now - last) * 1000 < self._dedup_window_ms:
            logger.warning("[TraceRecorder] 重复 llm_end 被跳过: run_id=%s", rid)
            return
        self._recent_llm_events[dedup_key] = now

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
                            "(step_id,seq,role,content,tool_calls,tool_call_id,reasoning) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (sid, seq, d["role"], d["content"],
                             d["tool_calls"], d["tool_call_id"], d.get("reasoning")),
                        )
                        seq += 1
                    else:
                        parts.append({"role": "assistant", "content": g.text})

            from utils.token_usage import extract_token_usage
            usage = extract_token_usage(response)
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
        node = metadata.get("node", "")
        langgraph_node = metadata.get("langgraph_node", "")
        extra = {}
        effective_node = node or langgraph_node
        if effective_node:
            extra["label"] = _GRAPH_NODE_LABELS.get(effective_node, effective_node)
            extra["node"] = effective_node
        pdor_context = metadata.get("pdor_context", "")
        if pdor_context:
            extra["pdor_context"] = pdor_context

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


# 注册全局 hook：LangChain 每次创建 CallbackManager 时自动检查
# _active_recorder 非 None 则注入，实现零侵入 trace
try:
    from langchain_core.tracers.context import register_configure_hook
    register_configure_hook(_active_recorder, inheritable=True)
except ImportError:
    logger.warning("[TraceRecorder] register_configure_hook 不可用，trace 将不会自动注入")

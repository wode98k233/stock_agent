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
    _GRAPH_NODE_LABELS,
    _connect,
    _ensure_db,
    _guard,
    _identify_graph_node,
    _json,
    _msg_info,
    _now,
    _pick_name,
    ensure_trace_schema,
)

# 全局 ContextVar：AgentRunContext 设置后，所有 LLM 调用自动 trace
_active_recorder: ContextVar[Optional["TraceRecorder"]] = ContextVar(
    "active_trace_recorder", default=None
)


class TraceRecorder(BaseCallbackHandler):
    name = "TraceRecorder"
    raise_error = False

    def __init__(self, agent_name: str = "unknown", db_path: str | None = None,
                 db_adapter=None):
        super().__init__()
        self.agent_name = agent_name
        self._root: Optional[str] = None
        self._run_created: bool = False
        self._sid: Dict[str, int] = {}
        self._t: Dict[str, float] = {}
        self._open_steps: set = set()   # 尚未 finish 的 step（exclude_elapsed 要平移其起点）
        self._excluded_ms: float = 0.0  # 扣除的等待耗时（澄清反问等阻塞）
        self._lock = threading.RLock()
        self._recent_llm_events: Dict[str, float] = {}
        self._dedup_window_ms = 100

        # db_path=None → 当月月库（分库默认路由，run 从 start_run 起固定该库，跨月不漂移）。
        # 不能用裸相对路径做默认值：CWD 相对路径会在启动目录再生成一份 trace 库，
        # 造成数据分裂（曾出现项目根目录游离 agent_trace.db 空壳）。
        if db_path is None:
            from utils.agent_trace.store import store
            db_path = store.current_month_db()
            full_schema = False
        else:
            full_schema = True
        self._db = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._db_adapter = db_adapter

        if db_adapter is not None:
            self._ddl_ran = True
        else:
            self._ddl_ran: bool = False
            if str(self._db) != ":memory:":
                _ensure_db(self._db, full=full_schema)
                self._ddl_ran = True

    def _get_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            if not self._ddl_ran:
                ensure_trace_schema(self._conn)
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
            self._open_steps.add(rid)
            c.commit()
            return sid

    def _finish_step(self, run_id, status="success", output=None, error=""):
        rid = str(run_id)
        ms = self._ms(rid)
        sid = self._sid.get(rid)
        self._open_steps.discard(rid)
        if not sid:
            return
        with self._lock:
            c = self._get_conn()
            if status == "error":
                # 失败也要保留 output：工具自身正常返回、但内容判定为失败时，
                # 输出体是唯一能定位根因的证据（原先此处丢 output → 无法回溯）。
                c.execute(
                    "UPDATE steps SET error=?,output=?,status='error',"
                    "finished_at=?,duration_ms=? WHERE id=?",
                    (str(error), _json(output) if output is not None else None,
                     _now(), ms, sid),
                )
            else:
                c.execute(
                    "UPDATE steps SET output=?,status='success',"
                    "finished_at=?,duration_ms=? WHERE id=?",
                    (_json(output), _now(), ms, sid),
                )
            c.commit()

    def update_tool_step_metrics(self, run_id, metrics: dict) -> None:
        """压缩埋点：把工具输出压缩指标回填到 tool step 的 extra。

        on_tool_end 触发于 tool.ainvoke 返回时（早于 _run_one 的压缩），
        因此 steps.output 存的是压缩前原文；final_len/compress_method 需由
        BaseToolNode 压缩完成后回填（event_run_id 与 run_id 一致）。
        """
        rid = str(run_id)
        sid = self._sid.get(rid)
        if not sid:
            return
        try:
            with self._lock:
                c = self._get_conn()
                row = c.execute("SELECT extra FROM steps WHERE id=?", (sid,)).fetchone()
                extra = {}
                if row and row["extra"]:
                    try:
                        extra = json.loads(row["extra"])
                    except Exception:
                        extra = {}
                extra["compress"] = metrics
                c.execute(
                    "UPDATE steps SET extra=? WHERE id=?",
                    (json.dumps(extra, ensure_ascii=False), sid),
                )
                c.commit()
        except Exception:
            pass

    def start_run(self, task_input="") -> str:
        self._root = str(uuid4())
        self._run_created = True
        self._run_t0 = time.perf_counter()
        self._excluded_ms = 0.0
        self._open_steps = set()
        with self._lock:
            c = self._get_conn()
            c.execute(
                "INSERT OR IGNORE INTO runs(id,agent_name,input,status,created_at) "
                "VALUES(?,?,?,'running',?)",
                (self._root, self.agent_name, _json(task_input), _now()),
            )
            c.commit()
        return self._root

    def exclude_elapsed(self, seconds: float) -> None:
        """把阻塞等待（如澄清反问等用户交互）从 run 与进行中 step 的耗时里剔除。

        不剔除的话：一次 5 分钟的等待会被记成「任务跑了 5 分钟」，虚增
        runs.duration_ms / 对话耗时统计，并把 BudgetController 的时间预算吃光。
        """
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            return
        if sec <= 0:
            return
        self._excluded_ms = getattr(self, "_excluded_ms", 0.0) + sec * 1000
        # 进行中的 step 起点整体后移（等待期间新建的 step 不受影响）
        for rid in list(getattr(self, "_open_steps", ())):
            if rid in self._t:
                self._t[rid] += sec

    def end_run(self, output="", status="success", error=""):
        if not self._root:
            return
        duration_ms = None
        if hasattr(self, '_run_t0') and self._run_t0:
            duration_ms = round(
                (time.perf_counter() - self._run_t0) * 1000
                - getattr(self, "_excluded_ms", 0.0), 2)
            duration_ms = max(0.0, duration_ms)
        with self._lock:
            c = self._get_conn()
            c.execute(
                "UPDATE runs SET output=?,status=?,error=?,finished_at=?,duration_ms=? "
                "WHERE id=?",
                (_json(output), status, error, _now(), duration_ms, self._root),
            )
            c.commit()

    def record_cache_hit(self, *, label: str = "", model_tier: str = "standard",
                         saved_tokens: int = 0, response_head: str = "") -> Optional[int]:
        """A8 语义缓存命中：补记一条 llm 步骤（无真实 LLM 调用），extra 带 cache_hit=true。

        供网关在缓存命中短路（跳过 LLM）时调用，让 trace 能展示「cache_hit=true 且无
        LLM 调用」（AC-1）。无活动 run（_root 为空）时静默跳过；使用独立 event_run_id，
        不覆盖真实 LLM 步骤的状态槽位。
        """
        if not self._root:
            return None
        rid = str(uuid4())
        extra = {
            "cache_hit": True,
            "model_tier": model_tier,
            "saved_tokens": int(saved_tokens or 0),
            "label": label,
        }
        sid = self._create_step(rid, None, "llm", f"cache_hit[{label}]",
                                {"response_head": (response_head or "")[:200]}, extra)
        with self._lock:
            c = self._get_conn()
            c.execute(
                "UPDATE steps SET status='success', output=?, finished_at=?, duration_ms=0 "
                "WHERE id=?",
                (_json({"response_head": (response_head or "")[:200]}), _now(), sid),
            )
            c.commit()
        return sid

    def record_skill_event(self, kind: str, data: dict,
                           run_id: str | None = None) -> Optional[int]:
        """记录技能生命周期结构化事件（select/activate/switch/gap_reselect）。

        供 Agent 节点在技能决策点调用；无活动 run 时静默跳过。
        事件以 step_type='skill' 落库，extra 保留完整 payload，评审侧据此切片。
        run_id 仅作为 input 元数据，不改变事件归属 run（归属始终为活动 run）。
        data 先归一化为 JSON-safe（非 mapping/非序列化值转字符串），任何 payload 都不抛异常；
        kind/label 以调用参数为准，payload 同名字段不得覆盖。
        """
        if self._root is None:
            return None
        try:
            loaded = json.loads(
                json.dumps(data or {}, ensure_ascii=False, default=str)
            )
            # 非 mapping payload（str/int/list 等）兜底为 _raw，保证 extra/output 结构稳定
            safe_data = loaded if isinstance(loaded, dict) else {"_raw": str(loaded)}
        except Exception:
            # 循环引用等极端 payload 兜底，保证不向调用方抛异常
            safe_data = {"_raw": str(data)}
        rid = str(uuid4())
        label = {
            "select": "技能选择",
            "activate": "技能激活",
            "switch": "技能切换",
            "gap_reselect": "技能重选",
        }.get(kind, kind)
        extra = {**safe_data, "kind": kind, "label": label}
        sid = self._create_step(rid, None, "skill", kind,
                                {"run_id": run_id or self._root}, extra)
        with self._lock:
            c = self._get_conn()
            c.execute(
                "UPDATE steps SET status='success', output=?, finished_at=?, duration_ms=0 "
                "WHERE id=?",
                # output 与 extra 同源，kind/label 与 step_name 保持一致
                (_json(extra), _now(), sid),
            )
            c.commit()
        return sid

    def record_clarify(self, *, wait_s: float = 0.0, question_count: int = 0,
                       answered: int = 0, skipped: int = 0,
                       timed_out: bool = False, rejected: bool = False) -> Optional[int]:
        """记录一次澄清反问交互（step_type='clarify'）。

        等待时长已被 ``exclude_elapsed`` 从 run/step 耗时里剔除，因此这里刻意
        不写 duration_ms（保持 0），把真实等待放 extra.wait_s —— 既不虚增任务
        耗时，又能事后回答「用户卡了多久 / 超时了几次 / 跳过几题」。
        """
        if not self._root:
            return None
        extra = {
            "kind": "clarify",
            "label": "澄清反问",
            "wait_s": round(float(wait_s or 0), 2),
            "question_count": int(question_count or 0),
            "answered": int(answered or 0),
            "skipped": int(skipped or 0),
            "timed_out": bool(timed_out),
            "rejected": bool(rejected),
            "outcome": ("rejected" if rejected else
                        "timeout" if timed_out else
                        "answered" if answered else "skipped_all"),
        }
        rid = str(uuid4())
        sid = self._create_step(rid, None, "clarify", "澄清反问",
                                {"question_count": int(question_count or 0)}, extra)
        with self._lock:
            c = self._get_conn()
            c.execute(
                "UPDATE steps SET status='success', output=?, finished_at=?, duration_ms=0 "
                "WHERE id=?",
                (_json(extra), _now(), sid),
            )
            c.commit()
        return sid

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
                    # A8 缓存命中步骤非真实 LLM 调用，不计入调用次数与 token
                    if extra.get("cache_hit"):
                        continue
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

        # A8 计量字段（网关在 metadata 注入 cache_hit/model_tier/saved_tokens），
        # 只加字段不改 steps 结构
        for _k in ("cache_hit", "model_tier", "saved_tokens"):
            if _k in metadata:
                extra[_k] = metadata[_k]

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
        # 与 ExecutionStateCallback.on_tool_end 同口径地取内容（暴露 .content 时优先）
        if isinstance(output, str):
            output_str = output
        else:
            content = getattr(output, "content", None)
            output_str = content if isinstance(content, str) else str(output)

        # 工具常常把异常包装成普通文本/JSON 返回（既不抛异常，也就不会走 on_tool_error），
        # 因此必须按输出内容判定成败，否则失败会被记成 success（观测盲区）。
        from utils.tool_result import judge_tool_output

        failed, reason = judge_tool_output(output_str)
        if failed:
            self._finish_step(run_id, status="error", error=reason, output=output_str)
        else:
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


def get_active_recorder() -> Optional["TraceRecorder"]:
    """获取当前上下文的活动 TraceRecorder（无则 None）。

    供网关（A8 缓存命中补记）等外部模块零侵入访问；与下方 register_configure_hook
    使用同一 ContextVar，行为一致。
    """
    return _active_recorder.get()


# 注册全局 hook：LangChain 每次创建 CallbackManager 时自动检查
# _active_recorder 非 None 则注入，实现零侵入 trace
try:
    from langchain_core.tracers.context import register_configure_hook
    register_configure_hook(_active_recorder, inheritable=True)
except ImportError:
    logger.warning("[TraceRecorder] register_configure_hook 不可用，trace 将不会自动注入")

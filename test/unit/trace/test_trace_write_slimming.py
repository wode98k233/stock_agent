"""写入端瘦身的行为与体积护栏（trace 存储 T1）。

设计：``docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md``

三条断言线：
1. **llm 步**：``steps.input`` 只存引用摘要，读侧由 ``messages`` 无损重建；
2. **节点步**：``steps.input/output`` 存 state 摘要，但标量字段原样保留
   （``select_template`` 的 ``selected_template_id`` 被 quality_service 直接消费）；
3. **体积护栏**：一次典型对话（13 LLM + 8 工具 + 40 节点）的 trace 体积上限，
   防止后人把整份 state 又 dump 回去（那会让体积无任何测试失败地涨 10 倍）。
"""
import json
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

_project_root = str(Path(__file__).parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.agent_trace import TraceRecorder
from utils.agent_trace.db_adapter import SQLiteAdapter

# 单个工具输出 ~8KB（实测 mx_data_query 返回 7~10KB）
_TOOL_PAYLOAD = "R" * 8000


def _tmp_db():
    return Path(tempfile.gettempdir()) / f"test_trace_slim_{uuid.uuid4().hex[:8]}.db"


def _cleanup(db_path: Path):
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except PermissionError:
            pass


def _rows(r, sql):
    # 写入端会把长文本压成 blob 哨兵，断言一律按"读侧视角"（装行工厂）取值
    from utils.agent_trace.blobstore import install_row_factory

    conn = r._get_conn()
    install_row_factory(conn)
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _msg(role, content):
    return SimpleNamespace(type=role, content=content, tool_calls=None, tool_call_id=None)


def _big_state(step_index=0):
    """与实测分布同构的图 state：累积容器键占 88%，另有少量标量。"""
    return {
        "messages": [{"role": "human", "content": "M" * 4000} for _ in range(6)],
        "tool_calls": [{"name": "mx_data_query", "args": {"query": f"q{i}"}, "result": _TOOL_PAYLOAD}
                       for i in range(8)],
        "dispatch_history": [{"round": i, "detail": "D" * 600} for i in range(20)],
        "plan_steps": [{"step": f"s{i}", "skill": "mx_data"} for i in range(10)],
        "original_plan": {"steps": [{"i": i} for i in range(20)]},
        "accumulated_data": {"alpha": "A" * 8000},
        "step_results": [{"result": "S" * 2000} for _ in range(3)],
        "current_step_index": step_index,
        "template_id": "market_daily",
        "constraints": ["报告为收盘点评", "不输出个股买卖建议"],
        "input": "收盘啦，收盘啦，点评下今天大盘",
    }


# ============================================================
# 1. LLM 步：摘要 + 读侧重建
# ============================================================

def test_llm_step_input_is_summary_and_restores_from_messages():
    db_path = _tmp_db()
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root = r.start_run("点评下今天大盘")
        prompts = [
            _msg("system", "S" * 30000),
            _msg("human", "点评下今天大盘"),
            _msg("ai", "A" * 3000),
        ]
        r.on_llm_start({"name": "test_model", "kwargs": {"model_name": "m"}},
                       [prompts], run_id="llm-1", parent_run_id=root)

        row = _rows(r, "SELECT input FROM steps WHERE step_type='llm'")[0]
        summary = json.loads(row["input"])
        assert summary["_kind"] == "llm_prompt_ref"
        assert summary["msg_count"] == 3
        assert len(row["input"]) < 2000, "llm 步仍在写整份 prompt"
        assert "S" * 5000 not in row["input"]

        # messages 行仍是原文（无损地基）
        msgs = _rows(r, "SELECT role, content FROM messages ORDER BY seq")
        assert [m["role"] for m in msgs] == ["system", "human", "ai"]
        assert msgs[0]["content"] == "S" * 30000
        r.close()

        # 读侧：SQLiteAdapter（导出 / CLI 走这条）必须还原成逐条消息
        adapter = SQLiteAdapter(str(db_path))
        steps = adapter.get_steps_with_messages(root)
        llm_step = [s for s in steps if s["step_type"] == "llm"][0]
        restored = json.loads(llm_step["input"])
        assert [m["role"] for m in restored] == ["system", "human", "ai"]
        assert restored[0]["content"] == "S" * 30000
        adapter.close()
    finally:
        _cleanup(db_path)


# ============================================================
# 2. 节点步：state 摘要
# ============================================================

def test_chain_step_input_is_state_digest_without_accumulator_content():
    db_path = _tmp_db()
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root = r.start_run("q")
        r.on_chain_start({"name": "plan_node"}, _big_state(), run_id="node-1", parent_run_id=root)

        row = _rows(r, "SELECT input FROM steps WHERE event_run_id='node-1'")[0]
        digest = json.loads(row["input"])
        assert digest["_kind"] == "state_digest"
        assert digest["_acc"]["tool_calls"]["count"] == 8
        assert digest["_acc"]["messages"]["count"] == 6
        assert digest["_orig_chars"] > 100000
        # 累积容器的内容体一个字节都不许进 steps.input
        assert _TOOL_PAYLOAD[:500] not in row["input"]
        assert "M" * 1000 not in row["input"]
        assert "A" * 1000 not in row["input"]
        # 标量/小容器原样保留
        assert digest["current_step_index"] == 0
        assert digest["constraints"] == ["报告为收盘点评", "不输出个股买卖建议"]
        r.close()
    finally:
        _cleanup(db_path)


def test_chain_step_output_keeps_selected_template_id():
    """回归：server/quality_service.py:573 直接读 steps.output 的 selected_template_id。"""
    db_path = _tmp_db()
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root = r.start_run("q")
        outputs = {
            "selected_template_id": "market_daily",
            "selected_template": {"id": "market_daily", "role": "R" * 20000},
        }
        r.on_chain_start({"name": "select_template"}, {"q": "x"}, run_id="tpl-1", parent_run_id=root)
        r.on_chain_end(outputs, run_id="tpl-1")
        r.close()

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        raw = conn.execute(
            "SELECT output FROM steps WHERE event_run_id='tpl-1'"
        ).fetchone()[0]
        conn.close()
        data = json.loads(raw)
        assert data["selected_template_id"] == "market_daily"
        assert "R" * 5000 not in raw  # 22KB 模板体被裁掉
        assert data["_orig_chars"] > 20000
    finally:
        _cleanup(db_path)


def test_tool_step_input_kept_verbatim():
    """工具入参必须原样保留（9 处读点里有 5 处直接消费它）。"""
    db_path = _tmp_db()
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root = r.start_run("q")
        args = {"query": "上证指数、深证成指 今日收盘点位、涨跌幅、成交额"}
        r.on_tool_start({"name": "mx_data_query"}, json.dumps(args, ensure_ascii=False),
                        run_id="tool-1", parent_run_id=root)
        row = _rows(r, "SELECT input FROM steps WHERE event_run_id='tool-1'")[0]
        assert json.loads(row["input"]) == args
        r.close()
    finally:
        _cleanup(db_path)


# ============================================================
# 3. 体积护栏
# ============================================================

def _file_bytes(db_path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def test_typical_run_trace_size_budget():
    """一次典型对话（13 LLM + 8 工具 + 40 节点）的 trace 体积护栏。

    现状（未瘦身）实测 2.64 MB；瘦身后必须 < 1.2 MB。
    同时校验"反事实体积"（把整份 state 全量序列化会有多大），确保这条护栏真的
    在防回归 —— 若有人改回全量 dump，反事实比会瞬间跌破 4 倍。
    """
    db_path = _tmp_db()
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root = r.start_run("收盘啦，点评下今天大盘")
        state = _big_state()
        counterfactual = 0

        history = [_msg("system", "S" * 12000), _msg("human", "收盘啦，点评下今天大盘")]
        for i in range(13):
            history.append(_msg("ai", f"answer-{i}" + "A" * 1000))
            prompts = [list(history)]
            counterfactual += len(json.dumps([[m.content for m in history]], ensure_ascii=False))
            r.on_llm_start({"name": f"m{i}", "kwargs": {"model_name": "m"}},
                           prompts, run_id=f"llm-{i}", parent_run_id=root)

        for i in range(8):
            r.on_tool_start({"name": "mx_data_query"}, json.dumps({"query": f"q{i}"}),
                            run_id=f"tool-{i}", parent_run_id=root)
            r.on_tool_end(_TOOL_PAYLOAD, run_id=f"tool-{i}")
            state["tool_calls"].append({"name": "mx_data_query", "result": _TOOL_PAYLOAD})
            state["accumulated_data"][f"k{i}"] = _TOOL_PAYLOAD
            state["current_step_index"] = i + 1

        for i in range(40):
            # 每个节点都把整份 state 交给 trace（真实图行为），并在末尾回吐 state
            counterfactual += len(json.dumps(state, ensure_ascii=False, default=str)) * 2
            r.on_chain_start({"name": f"node{i}"}, dict(state),
                             run_id=f"node-{i}", parent_run_id=root)
            r.on_chain_end(dict(state), run_id=f"node-{i}")

        r.close()
        size = _file_bytes(db_path)
        assert size < 1_200_000, f"trace 体积 {size/1024:.0f}KB 超出护栏 1.2MB"
        # 护栏有效性自检：不瘦身的话 GB 级差距，至少 4 倍
        assert counterfactual > size * 4, "反事实体积比过小，护栏未真正生效"
    finally:
        _cleanup(db_path)

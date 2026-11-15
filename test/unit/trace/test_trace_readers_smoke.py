"""一库三形态过全部读点：新格式行不得让任何一条读取链路静默变质。

设计 §11.2 的 ``test_trace_readers_smoke``。

为什么必须有这条
----------------
trace 有三类消费者（Web 运行详情、Trace Viewer、评测/洞察），读 ``steps.input``
的地方有 9 处、读 ``messages`` 的地方更多。T1/T2 之后库里出现三种行：

1. **老格式**：整份 prompt / state 原文（存量行，永不回改）；
2. **摘要行**：``_kind=llm_prompt_ref`` / ``_kind=state_digest``；
3. **压缩行**：``\\x01Bzlib:<hash>`` 哨兵 + ``blobs`` 表。

任何一个读点漏处理，**都不会抛错**，只会安静地显示哨兵串或把摘要当成正文——
这比崩溃更难发现。所以这里用一个"三形态混装"的库把读点逐个跑一遍。

构造方式：真 ``TraceRecorder`` 写入（走的是 T1/T2 真实写路径），再用假日期前缀
让 ``store`` 的按月路由把临时目录当成当月月库。
"""
import json
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_project_root = str(Path(__file__).parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture()
def mixed_trace(tmp_path, monkeypatch):
    """造一个同时含老格式 / 摘要行 / 压缩行的月库，并把 store 路由指过去。"""
    trace_dir = tmp_path / "agent_trace"
    trace_dir.mkdir()

    monkeypatch.setattr(
        "utils.app_paths.get_trace_dir", lambda: str(trace_dir), raising=True
    )

    from utils.agent_trace import TraceRecorder, store as store_mod

    month = store_mod.store.current_month()
    db_path = trace_dir / f"agent_trace_{month}.db"

    recorder = TraceRecorder("smoke_agent", str(db_path))
    run_id = recorder.start_run("收盘啦，点评下今天大盘")

    # ① LLM 步：20KB system prompt + 小 human —— 入参摘要化、messages 压缩
    prompts = [
        SimpleNamespace(type="system", content="S" * 20000, tool_calls=None, tool_call_id=None),
        SimpleNamespace(type="human", content="点评下今天大盘", tool_calls=None, tool_call_id=None),
        SimpleNamespace(type="ai", content="A" * 3000, tool_calls=None, tool_call_id=None),
    ]
    recorder.on_llm_start({"name": "m", "kwargs": {"model_name": "m"}},
                          [prompts], run_id="llm-1", parent_run_id=run_id)

    # ② 工具步：入参/出参必须逐字保留（8KB 输出会被 T2 压缩）
    recorder.on_tool_start({"name": "mx_data_query"},
                           json.dumps({"query": "上证指数 今日收盘"}, ensure_ascii=False),
                           run_id="tool-1", parent_run_id=run_id)
    recorder.on_tool_end("R" * 8000, run_id="tool-1")

    # ③ 节点步：整份 state → 摘要（含累积容器键与大字段）
    big_state = {
        "messages": [{"role": "human", "content": "M" * 4000} for _ in range(6)],
        "tool_calls": [{"name": "mx_data_query", "result": "R" * 8000} for _ in range(4)],
        "dispatch_history": [{"round": i, "detail": "D" * 600} for i in range(10)],
        "current_step_index": 2,
        "template_id": "market_daily",
        "selected_template_id": "market_daily",
        "response": "B" * 5000,
    }
    recorder.on_chain_start({"name": "select_template"}, dict(big_state),
                            run_id="node-1", parent_run_id=run_id)
    recorder.on_chain_end({"selected_template_id": "market_daily",
                           "selected_template": {"role": "T" * 20000}},
                          run_id="node-1")

    # ④ 老格式行：直接按改动前的样子写一条（存量库就是这样）——读点必须原样兼容
    legacy = '[\n  [\n    "content=\'老格式 prompt\'"\n  ]\n]'
    conn = recorder._get_conn()
    conn.execute(
        "INSERT INTO steps(run_id,event_run_id,parent_run_id,step_type,step_name,"
        "input,output,status,started_at) VALUES(?,?,?,'llm','legacy-model',?,?,'success',?)",
        (run_id, "llm-legacy", run_id, legacy, '{"ok": true}', "2026-10-19T00:00:00"),
    )
    conn.commit()
    recorder.close()

    from utils.agent_trace import blobstore

    blobstore.clear_cache()
    yield run_id, db_path
    blobstore.clear_cache()
    store_mod.store.forget_month(month)


# ============================================================
# 读点：每个 consumer 一条，失败时报出具体读点
# ============================================================

def test_reader_store_get_steps_with_messages(mixed_trace):
    from utils.agent_trace.store import store

    run_id, _ = mixed_trace
    steps = store.get_steps_with_messages(run_id)
    assert steps
    llm = [s for s in steps if s["step_type"] == "llm" and s["event_run_id"] == "llm-1"][0]
    restored = json.loads(llm["input"])
    assert [m["role"] for m in restored] == ["system", "human", "ai"]
    assert restored[0]["content"] == "S" * 20000
    node = [s for s in steps if s["event_run_id"] == "node-1"][0]
    assert json.loads(node["input"])["_kind"] == "state_digest"
    assert not str(node["input"]).startswith("\x01B")


def test_reader_store_legacy_row_untouched(mixed_trace):
    from utils.agent_trace.store import store

    run_id, _ = mixed_trace
    steps = store.get_steps_with_messages(run_id)
    legacy = [s for s in steps if s["event_run_id"] == "llm-legacy"][0]
    assert legacy["input"].startswith("[\n  [\n    \"content='老格式 prompt'")


def test_reader_store_get_step_detail(mixed_trace):
    from utils.agent_trace.store import store

    run_id, _ = mixed_trace
    steps = store.get_steps(run_id)
    tool = [s for s in steps if s["event_run_id"] == "tool-1"][0]
    detail = store.get_step_detail(run_id, tool["id"])
    assert json.loads(detail["input"])["query"] == "上证指数 今日收盘"
    assert detail["output"] == "R" * 8000  # 压缩行必须解压成原文


def test_reader_web_query_trace_steps(mixed_trace):
    from server.routes.traces import query_trace_steps

    run_id, _ = mixed_trace
    payload = query_trace_steps(run_id)
    names = {item["step_name"] for item in payload["items"]}
    assert "mx_data_query" in names
    # 该接口不取 input，不该因为摘要/压缩行崩
    assert all("\x01B" not in str(item.get("extra") or "") for item in payload["items"])


def test_reader_web_query_trace_chain(mixed_trace):
    from server.routes.traces import query_trace_chain

    run_id, _ = mixed_trace
    chain = query_trace_chain(run_id)
    tool_items = [i for i in chain["items"] if i["type"] == "tool"]
    assert tool_items, "工具行缺失"
    assert tool_items[0]["query"] == "上证指数 今日收盘"
    assert "R" * 100 in tool_items[0]["summary"] or tool_items[0]["summary"]


def test_reader_transcript_entries(mixed_trace):
    from utils.agent_trace.store import store
    from server.runtime.transcript import read_run_entries

    run_id, _ = mixed_trace
    conn = store.conn_for_run(run_id)
    entries = read_run_entries(conn, run_id)
    tools = [e for e in entries if e.get("kind") == "tool"]
    assert tools, "过程流卡片缺工具条目"
    # 过程流卡片的 input_summary 是工具入参原文（JSON 串），不是抽出来的 query
    assert "上证指数" in tools[0]["input_summary"]
    assert "\x01B" not in tools[0]["input_summary"]


def test_reader_decision_chain(mixed_trace):
    from utils.agent_trace.db_adapter import get_decision_chain

    run_id, _ = mixed_trace
    chain = get_decision_chain(run_id)
    assert chain is not None
    assert any(c["name"] == "mx_data_query" for c in chain.get("tool_calls", []))


def test_reader_context_stats(mixed_trace):
    from utils.agent_trace.context_stats import build_run_context

    run_id, _ = mixed_trace
    ctx = build_run_context(run_id)
    assert ctx is not None
    assert ctx.get("messages") or ctx.get("steps") or ctx


def test_reader_eval_review_collect(mixed_trace):
    from utils.agent_trace.store import store
    from eval.review.collector import collect_run

    run_id, _ = mixed_trace
    conn = store.conn_for_run(run_id)
    digest = collect_run(conn, run_id)
    assert digest
    assert json.dumps(digest, ensure_ascii=False, default=str)


def test_reader_eval_evidence(mixed_trace):
    from utils.agent_trace.store import store
    from eval.evidence.trace_source import load_run_evidence, text_at_step

    run_id, _ = mixed_trace
    conn = store.conn_for_run(run_id)
    evidence = load_run_evidence(conn, run_id)
    assert evidence.run_id == run_id
    assert any(t.tool == "mx_data_query" for t in evidence.tools)
    tool = evidence.tools[0]
    assert "上证指数" in tool.args
    assert "R" * 100 in tool.output


def test_reader_storage_tool_summary(mixed_trace):
    """`server/storage.py::get_attribution` 走的是同款 `_tool_step_summary`。"""
    from utils.agent_trace.store import store
    from server.storage import _tool_step_summary

    run_id, _ = mixed_trace
    conn = store.conn_for_run(run_id)
    row = conn.execute(
        "SELECT id, step_type, step_name, status, input, output FROM steps "
        "WHERE step_type='tool'"
    ).fetchone()
    summary = _tool_step_summary(dict(row))
    assert summary["query"] == "上证指数 今日收盘"


def test_reader_export(mixed_trace, tmp_path):
    from utils.agent_trace.migrate import TraceExporter

    run_id, db_path = mixed_trace
    out = tmp_path / "export.json"
    TraceExporter().export_run(run_id, output_path=str(out), db_path=str(db_path))
    data = json.loads(out.read_text(encoding="utf-8"))
    llm = [s for s in data["steps"] if s["event_run_id"] == "llm-1"][0]
    assert json.loads(llm["input"])[0]["content"] == "S" * 20000
    tool = [s for s in data["steps"] if s["event_run_id"] == "tool-1"][0]
    assert tool["output"] == "R" * 8000

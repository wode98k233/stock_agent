"""
agent_trace 模块重构验证测试

测试目标：
1. TraceRecorder 核心功能（start_run/end_run/close）
2. _create_step / _finish_step 公共方法
3. 四种回调 handler（chain/llm/tool/retriever）
4. _guard 改造后行为
5. _identify_graph_node 节点识别
6. TracedLLM / patch_llm
7. CLI 命令（ls/show/tree/stats/search/export）
8. server 模块（serve）

运行方式：
  d:\Coding\Python\stock-solve\.venv\Scripts\python.exe -m pytest test/unit/test_agent_trace.py -v
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================
# 1. TraceRecorder 核心功能
# ============================================================

def test_trace_recorder_instantiation():
    """验证 TraceRecorder 可以正常实例化"""
    from utils.agent_trace import TraceRecorder
    r = TraceRecorder("test_agent", ":memory:")
    assert r.agent_name == "test_agent"
    assert r._root is None
    r.close()


def test_start_run_returns_root_id():
    """验证 start_run 返回有效的 root run_id"""
    from utils.agent_trace import TraceRecorder
    r = TraceRecorder("test_agent", ":memory:")
    rid = r.start_run("test input")
    assert rid is not None
    assert isinstance(rid, str)
    assert len(rid) > 10
    assert r._root == rid
    r.close()


def test_end_run_no_error():
    """验证 end_run 不会抛出异常"""
    from utils.agent_trace import TraceRecorder
    r = TraceRecorder("test_agent", ":memory:")
    r.start_run("input")
    r.end_run("output", status="success")
    r.close()


def test_start_end_run_writes_to_db():
    """验证 start_run + end_run 实际写入了 runs 表"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_trace_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        rid = r.start_run("hello")
        r.end_run("world")
        r.close()

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        assert row is not None
        assert row[1] == "test_agent"  # agent_name
        assert row[4] == "success"     # status
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


# ============================================================
# 2. _create_step / _finish_step 公共方法
# ============================================================

def test_create_step_returns_step_id():
    """验证 _create_step 返回有效的 step_id"""
    from utils.agent_trace import TraceRecorder
    r = TraceRecorder("test_agent", ":memory:")
    r._root = r.start_run("input")
    sid = r._create_step("run-1", None, "chain", "test_step", "hello")
    assert sid is not None
    assert isinstance(sid, int)
    assert sid > 0
    r.close()


def test_finish_step_updates_status():
    """验证 _finish_step 更新了状态为 success"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_trace_finish_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        r._root = r.start_run("input")
        sid = r._create_step("run-1", None, "chain", "test_step", "hello")
        r._finish_step("run-1", status="success", output="done")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM steps WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "success"
        assert row["output"] is not None
        assert row["finished_at"] is not None
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_finish_step_error():
    """验证 _finish_step 错误状态写入 error 字段"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_trace_error_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        r._root = r.start_run("input")
        sid = r._create_step("run-2", None, "tool", "bad_tool", "data")
        r._finish_step("run-2", status="error", error="Something went wrong")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM steps WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "error"
        assert "Something went wrong" in str(row["error"])
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


# ============================================================
# 3. 四种回调 handler
# ============================================================

def test_on_chain_start_end():
    """验证 on_chain_start + on_chain_end 创建并完成步骤"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_chain_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        # 模拟 LangChain callback
        r.on_chain_start({"name": "test_chain"}, {"key": "val"},
                         run_id="chain-1", parent_run_id=None)
        r.on_chain_end({"result": "ok"}, run_id="chain-1")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM steps").fetchall()
        assert len(rows) >= 1
        step = [dict(r) for r in rows if r["event_run_id"] == "chain-1"][0]
        assert step["step_type"] == "graph"
        assert step["status"] == "success"
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_on_chain_error():
    """验证 on_chain_error 正确记录错误"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_chain_err_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        r.on_chain_start({"name": "failing_chain"}, {},
                         run_id="chain-err", parent_run_id=None)
        r.on_chain_error(ValueError("fail"), run_id="chain-err")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM steps").fetchall()
        step = [dict(r) for r in rows if r["event_run_id"] == "chain-err"][0]
        assert step["status"] == "error"
        assert "fail" in str(step["error"])
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_on_llm_start_end():
    """验证 on_llm_start + on_llm_end 记录 LLM 调用和 messages"""
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from types import SimpleNamespace

    db_path = Path(tempfile.gettempdir()) / f"test_llm_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root_rid = r.start_run("input")

        # 模拟 LLM start — 用 Message-like 对象确保 messages 被记录
        serialized = {"name": "test_model", "kwargs": {"model_name": "gpt-4"}}
        msg = SimpleNamespace(type="human", content="prompt1", tool_calls=None, tool_call_id=None)
        r.on_llm_start(serialized, [msg],
                       run_id="llm-1", parent_run_id=root_rid)

        # 模拟 LLM end
        response = LLMResult(
            generations=[[Generation(text="response1", generation_info=None)]],
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        )
        r.on_llm_end(response, run_id="llm-1")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        steps = conn.execute("SELECT * FROM steps").fetchall()
        llm_steps = [dict(s) for s in steps if s["event_run_id"] == "llm-1"]
        assert len(llm_steps) == 1
        assert llm_steps[0]["step_type"] == "llm"
        assert llm_steps[0]["status"] == "success"

        msgs = conn.execute("SELECT * FROM messages").fetchall()
        assert len(msgs) > 0
        assert msgs[0]["role"] == "human"
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_on_tool_start_end():
    """验证 on_tool_start + on_tool_end 记录工具调用"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_tool_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        rid = r.start_run("input")
        r.on_tool_start({"name": "get_stock_price"}, "600519",
                        run_id="tool-1", parent_run_id=rid)
        r.on_tool_end("189.50", run_id="tool-1")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        step = conn.execute(
            "SELECT * FROM steps WHERE event_run_id=?", ("tool-1",)
        ).fetchone()
        assert step is not None
        assert step["step_type"] == "tool"
        assert step["step_name"] == "get_stock_price"
        assert step["status"] == "success"
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_on_tool_error():
    """验证 on_tool_error 记录工具错误"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_tool_err_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        rid = r.start_run("input")
        r.on_tool_start({"name": "failing_tool"}, "data",
                        run_id="tool-err", parent_run_id=rid)
        r.on_tool_error(RuntimeError("API timeout"), run_id="tool-err")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        step = conn.execute(
            "SELECT * FROM steps WHERE event_run_id=?", ("tool-err",)
        ).fetchone()
        assert step["status"] == "error"
        assert "API timeout" in str(step["error"])
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_on_retriever_start_end():
    """验证 on_retriever_start + on_retriever_end 记录检索"""
    from utils.agent_trace import TraceRecorder
    from langchain_core.documents import Document

    db_path = Path(tempfile.gettempdir()) / f"test_ret_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        rid = r.start_run("input")
        r.on_retriever_start({"name": "stock_retriever"}, "茅台财报",
                             run_id="ret-1", parent_run_id=rid)
        r.on_retriever_end(
            [Document(page_content="茅台2024年财报", metadata={"year": 2024})],
            run_id="ret-1"
        )

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        step = conn.execute(
            "SELECT * FROM steps WHERE event_run_id=?", ("ret-1",)
        ).fetchone()
        assert step["step_type"] == "retriever"
        assert step["status"] == "success"
        assert "茅台" in str(step["output"])
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


def test_on_retriever_error():
    """验证 on_retriever_error 记录检索错误"""
    from utils.agent_trace import TraceRecorder
    db_path = Path(tempfile.gettempdir()) / f"test_ret_err_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))
        rid = r.start_run("input")
        r.on_retriever_start({"name": "failing_retriever"}, "query",
                             run_id="ret-err", parent_run_id=rid)
        r.on_retriever_error(Exception("retrieval failed"), run_id="ret-err")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        step = conn.execute(
            "SELECT * FROM steps WHERE event_run_id=?", ("ret-err",)
        ).fetchone()
        assert step["status"] == "error"
        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


# ============================================================
# 4. _guard 改造后行为
# ============================================================

def test_guard_logs_but_does_not_propagate():
    """验证 _guard 记录异常但不传播（不崩溃）"""
    from utils.agent_trace.models import _guard

    class TestHandler:
        @_guard
        def handle(self, x):
            raise ValueError(f"test error: {x}")

    h = TestHandler()
    # 不应该抛出异常
    h.handle("hello")
    # 多次调用也不应该抛出
    h.handle("world")


def test_guard_preserves_success():
    """验证 _guard 不影响正常返回"""
    from utils.agent_trace.models import _guard

    class TestHandler:
        @_guard
        def add(self, a, b):
            return a + b

    h = TestHandler()
    result = h.add(2, 3)
    assert result == 5


# ============================================================
# 5. _identify_graph_node 节点识别
# ============================================================

def test_identify_graph_node_root():
    """parent_run_id=None 应识别为 graph 根节点"""
    from utils.agent_trace.models import _identify_graph_node
    node_type, display, label, is_noise = _identify_graph_node({}, {}, parent_run_id=None)
    assert node_type == "graph"
    assert display == "LangGraph"
    assert is_noise is False


def test_identify_graph_node_langgraph():
    """有 langgraph_node metadata 的应识别为对应节点"""
    from utils.agent_trace.models import _identify_graph_node
    node_type, display, label, is_noise = _identify_graph_node(
        {}, {"metadata": {"langgraph_node": "planner"}}, parent_run_id="root"
    )
    assert node_type == "planner"
    assert display == "planner"
    assert label == "生成计划"
    assert is_noise is False


def test_identify_graph_node_agent():
    """含 agent 名称的应识别为 agent"""
    from utils.agent_trace.models import _identify_graph_node
    node_type, display, label, is_noise = _identify_graph_node(
        {"name": "my_agent"}, {}, parent_run_id="root"
    )
    assert node_type == "agent"
    assert is_noise is True


def test_identify_graph_node_undefined_noise():
    """未知节点且有 parent 的应标记为噪音"""
    from utils.agent_trace.models import _identify_graph_node
    node_type, display, label, is_noise = _identify_graph_node(
        {"name": "some_random_step"}, {}, parent_run_id="parent123"
    )
    assert node_type == "undefined"
    assert is_noise is True


# ============================================================
# 6. TracedLLM / patch_llm
# ============================================================

def test_patch_llm_returns_traced_llm():
    """验证 patch_llm 返回 TracedLLM 实例"""
    from utils.agent_trace import patch_llm, TracedLLM

    mock_llm = MagicMock()
    mock_recorder = MagicMock()
    result = patch_llm(mock_llm, mock_recorder)
    assert isinstance(result, TracedLLM)


def test_patch_llm_no_double_wrap():
    """验证 patch_llm 对已包装实例不重复包装"""
    from utils.agent_trace import patch_llm, TracedLLM

    mock_llm = MagicMock()
    mock_recorder = MagicMock()
    wrapped = TracedLLM(mock_llm, mock_recorder)
    result = patch_llm(wrapped, MagicMock())
    assert result is wrapped  # 应返回原实例


def test_traced_llm_invoke_passes_callbacks():
    """验证 TracedLLM.invoke 将 recorder 传入 callbacks"""
    from utils.agent_trace import TracedLLM

    mock_llm = MagicMock()
    mock_recorder = MagicMock()
    wrapped = TracedLLM(mock_llm, mock_recorder)

    result = wrapped.invoke("hello", config={"callbacks": []})
    # 验证 invoke 被调用，且 callbacks 包含 recorder
    call_kwargs = mock_llm.invoke.call_args
    assert call_kwargs is not None
    config = call_kwargs[1].get("config", {})
    callbacks = config.get("callbacks", [])
    assert mock_recorder in callbacks


# ============================================================
# 7. models 辅助函数
# ============================================================

def test_now_returns_iso_format():
    """验证 _now 返回 ISO 8601 格式"""
    from utils.agent_trace.models import _now
    result = _now()
    assert "T" in result
    assert result.endswith("+00:00") or "+" in result


def test_json_handles_none():
    """验证 _json(None) 返回空字符串"""
    from utils.agent_trace.models import _json
    assert _json(None) == ""


def test_json_handles_string():
    """验证 _json 直接返回字符串"""
    from utils.agent_trace.models import _json
    assert _json("hello") == "hello"


def test_json_handles_dict():
    """验证 _json 序列化 dict"""
    from utils.agent_trace.models import _json
    result = _json({"a": 1, "b": "test"})
    assert isinstance(result, str)
    assert "a" in result


def test_pick_name_from_name():
    """验证 _pick_name 从 name 字段取值"""
    from utils.agent_trace.models import _pick_name
    assert _pick_name({"name": "my_chain"}) == "my_chain"


def test_pick_name_from_id_list():
    """验证 _pick_name 从 id 列表取值"""
    from utils.agent_trace.models import _pick_name
    assert _pick_name({"id": ["a", "b", "c"]}) == "c"


def test_pick_name_from_model_kwargs():
    """验证 _pick_name 从 kwargs 中的 model_name 取值"""
    from utils.agent_trace.models import _pick_name
    assert _pick_name({"kwargs": {"model_name": "gpt-4"}}) == "gpt-4"


def test_type_icons_has_all_keys():
    """验证 _TYPE_ICONS 包含所有预期键"""
    from utils.agent_trace.models import _TYPE_ICONS
    expected_keys = {"llm", "tool", "chain", "retriever", "agent", "tools",
                     "planner", "executor", "replanner", "classifier", "graph", "undefined"}
    assert expected_keys.issubset(set(_TYPE_ICONS.keys()))


# ============================================================
# 8. CLI 命令
# ============================================================

def test_cli_ls_works():
    """验证 CLI ls 命令可以正常执行"""
    from utils.agent_trace.cli import main as cli_main
    import sys

    old_argv = sys.argv
    sys.argv = ["agent_trace", "ls", "--db", ":memory:"]
    try:
        cli_main()
    except SystemExit:
        pass
    finally:
        sys.argv = old_argv


def test_cli_serve_imports():
    """验证 serve 模块可以正常导入"""
    from utils.agent_trace.server import cmd_serve
    assert cmd_serve is not None


# ============================================================
# 9. 数据库 schema 完整性
# ============================================================

def test_ddl_creates_all_tables():
    """验证 DDL 创建所有必要的表"""
    from utils.agent_trace.models import _DDL
    assert "CREATE TABLE IF NOT EXISTS runs" in _DDL
    assert "CREATE TABLE IF NOT EXISTS steps" in _DDL
    assert "CREATE TABLE IF NOT EXISTS messages" in _DDL
    assert "CREATE INDEX IF NOT EXISTS idx_steps_run" in _DDL


# ============================================================
# 10. 集成场景：完整回调链
# ============================================================

def test_full_callback_chain():
    """模拟一个完整的 Agent 执行回调链，验证所有步骤正确记录"""
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from langchain_core.documents import Document
    from types import SimpleNamespace

    db_path = Path(tempfile.gettempdir()) / f"test_full_chain_{threading.get_ident()}.db"
    try:
        r = TraceRecorder("test_agent", str(db_path))

        # 模拟 Agent 执行流程：
        # 1. Start Run
        rid = r.start_run("分析茅台股票")

        # 2. Classifier → chain start
        r.on_chain_start(
            {"name": "classifier"},
            {"input": "分析茅台股票"},
            run_id="chain-cls", parent_run_id=rid,
            metadata={"langgraph_node": "classifier"}
        )
        r.on_chain_end({"classification": "stock_analysis"}, run_id="chain-cls")

        # 3. LLM call → llm start/end
        serialized = {"name": "chat_model", "kwargs": {"model_name": "gpt-4"}}
        msg = SimpleNamespace(type="human", content="分析茅台", tool_calls=None, tool_call_id=None)
        r.on_chat_model_start(
            serialized, [msg],
            run_id="llm-1", parent_run_id="chain-cls"
        )
        response = LLMResult(
            generations=[[Generation(text="茅台是好股票", generation_info=None)]],
            llm_output={"token_usage": {"prompt_tokens": 50, "completion_tokens": 20}}
        )
        r.on_llm_end(response, run_id="llm-1")

        # 4. Tool call → tool start/end
        r.on_tool_start(
            {"name": "get_realtime_price"}, "600519",
            run_id="tool-1", parent_run_id="chain-cls"
        )
        r.on_tool_end("189.50", run_id="tool-1")

        # 5. Retriever → retriever start/end
        r.on_retriever_start(
            {"name": "news_retriever"}, "茅台新闻",
            run_id="ret-1", parent_run_id="chain-cls"
        )
        r.on_retriever_end(
            [Document(page_content="茅台发布2024年报", metadata={"source": "eastmoney"})],
            run_id="ret-1"
        )

        # 6. End Run
        r.end_run("分析完成", status="success")
        r.close()

        # 验证数据库记录
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # 验证 runs
        run = dict(conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())
        assert run["status"] == "success"
        assert run["agent_name"] == "test_agent"

        # 验证 steps — 分类步骤
        steps = [dict(s) for s in conn.execute("SELECT * FROM steps").fetchall()]
        assert len(steps) >= 4  # classifier + llm + tool + retriever

        step_types = {s["step_type"] for s in steps}
        assert "llm" in step_types
        assert "tool" in step_types
        assert "retriever" in step_types

        # 验证所有步骤都不是 running 状态（都应该 completed/error）
        running = [s for s in steps if s["status"] == "running"]
        assert len(running) == 0, f"仍有 {len(running)} 个步骤未完成"

        # 验证 messages
        msgs = [dict(m) for m in conn.execute("SELECT * FROM messages").fetchall()]
        assert len(msgs) > 0

        conn.close()
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass


if __name__ == "__main__":
    import traceback
    tests = [
        ("test_trace_recorder_instantiation", test_trace_recorder_instantiation),
        ("test_start_run_returns_root_id", test_start_run_returns_root_id),
        ("test_end_run_no_error", test_end_run_no_error),
        ("test_start_end_run_writes_to_db", test_start_end_run_writes_to_db),
        ("test_create_step_returns_step_id", test_create_step_returns_step_id),
        ("test_finish_step_updates_status", test_finish_step_updates_status),
        ("test_finish_step_error", test_finish_step_error),
        ("test_on_chain_start_end", test_on_chain_start_end),
        ("test_on_chain_error", test_on_chain_error),
        ("test_on_llm_start_end", test_on_llm_start_end),
        ("test_on_tool_start_end", test_on_tool_start_end),
        ("test_on_tool_error", test_on_tool_error),
        ("test_on_retriever_start_end", test_on_retriever_start_end),
        ("test_on_retriever_error", test_on_retriever_error),
        ("test_guard_logs_but_does_not_propagate", test_guard_logs_but_does_not_propagate),
        ("test_guard_preserves_success", test_guard_preserves_success),
        ("test_identify_graph_node_root", test_identify_graph_node_root),
        ("test_identify_graph_node_langgraph", test_identify_graph_node_langgraph),
        ("test_identify_graph_node_agent", test_identify_graph_node_agent),
        ("test_identify_graph_node_undefined_noise", test_identify_graph_node_undefined_noise),
        ("test_patch_llm_returns_traced_llm", test_patch_llm_returns_traced_llm),
        ("test_patch_llm_no_double_wrap", test_patch_llm_no_double_wrap),
        ("test_traced_llm_invoke_passes_callbacks", test_traced_llm_invoke_passes_callbacks),
        ("test_now_returns_iso_format", test_now_returns_iso_format),
        ("test_json_handles_none", test_json_handles_none),
        ("test_json_handles_string", test_json_handles_string),
        ("test_json_handles_dict", test_json_handles_dict),
        ("test_pick_name_from_name", test_pick_name_from_name),
        ("test_pick_name_from_id_list", test_pick_name_from_id_list),
        ("test_pick_name_from_model_kwargs", test_pick_name_from_model_kwargs),
        ("test_type_icons_has_all_keys", test_type_icons_has_all_keys),
        ("test_cli_ls_works", test_cli_ls_works),
        ("test_cli_serve_imports", test_cli_serve_imports),
        ("test_ddl_creates_all_tables", test_ddl_creates_all_tables),
        ("test_full_callback_chain", test_full_callback_chain),
    ]
    passed, failed = 0, 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}")
    print(f"结果: {passed} passed, {failed} failed out of {len(tests)}")
    if failed > 0:
        sys.exit(1)

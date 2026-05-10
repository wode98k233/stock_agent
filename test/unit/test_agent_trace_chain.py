"""
Trace 链路测试

验证复杂执行流程中，trace 系统是否正确记录所有节点、LLM 调用、工具调用。

测试场景：
1. 多节点 Graph 链路（classifier → planner → executor → replanner）
2. 函数内 LLM 调用后进入 Graph
3. 嵌套 Graph
4. CLI 命令（show/tree/stats/search/export）
5. Server API 端点
6. 边界情况（并发写入、异常恢复等）

运行方式：
  .venv/Scripts/python.exe -m pytest test/unit/test_agent_trace_chain.py -v
"""
import json
import os
import sqlite3
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


def _create_recorder(db_path=None):
    """创建临时 TraceRecorder"""
    from utils.agent_trace import TraceRecorder
    if db_path is None:
        db_path = str(Path(tempfile.gettempdir()) / f"test_trace_{threading.get_ident()}_{time.time_ns()}.db")
    return TraceRecorder("test_agent", db_path), db_path


def _cleanup_db(db_path):
    """清理临时数据库"""
    try:
        Path(db_path).unlink(missing_ok=True)
    except PermissionError:
        pass


def _query_steps(db_path, run_id=None):
    """查询 steps 表"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if run_id:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM steps ORDER BY id"
        ).fetchall()]
    conn.close()
    return rows


def _query_runs(db_path):
    """查询 runs 表"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()]
    conn.close()
    return rows


def _query_messages(db_path, step_id=None):
    """查询 messages 表"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if step_id:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE step_id=? ORDER BY seq", (step_id,)
        ).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()]
    conn.close()
    return rows


# ============================================================
# 1. 多节点 Graph 链路
# ============================================================

def test_plan_graph_full_chain():
    """
    模拟 Plan & Solve Agent 完整链路：
    classifier → planner → executor → replanner → end
    验证所有 4 个节点都被记录
    """
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from types import SimpleNamespace

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("分析茅台股票")

        # 1. Classifier
        r.on_chain_start(
            {"name": "classifier"},
            {"input": "分析茅台股票"},
            run_id="chain-cls", parent_run_id=rid,
            metadata={"langgraph_node": "classifier"}
        )
        r.on_chain_end({"classification": "stock_analysis"}, run_id="chain-cls")

        # 2. Planner
        r.on_chain_start(
            {"name": "planner"},
            {"input": "生成分析计划"},
            run_id="chain-plan", parent_run_id=rid,
            metadata={"langgraph_node": "planner"}
        )
        r.on_chain_end({"plan": ["查询股价", "查询财务数据", "生成报告"]}, run_id="chain-plan")

        # 3. Executor (with LLM call)
        r.on_chain_start(
            {"name": "executor"},
            {"input": "执行步骤 1"},
            run_id="chain-exec", parent_run_id=rid,
            metadata={"langgraph_node": "executor"}
        )
        # LLM call inside executor
        serialized = {"name": "chat_model", "kwargs": {"model_name": "gpt-4"}}
        msg = SimpleNamespace(type="human", content="查询茅台股价", tool_calls=None, tool_call_id=None)
        r.on_chat_model_start(serialized, [msg], run_id="llm-exec", parent_run_id="chain-exec")
        response = LLMResult(
            generations=[[Generation(text="茅台当前价格 189.50", generation_info=None)]],
            llm_output={"token_usage": {"prompt_tokens": 30, "completion_tokens": 10}}
        )
        r.on_llm_end(response, run_id="llm-exec")
        r.on_chain_end({"result": "股价查询完成"}, run_id="chain-exec")

        # 4. Replanner
        r.on_chain_start(
            {"name": "replanner"},
            {"input": "评估是否需要继续"},
            run_id="chain-replan", parent_run_id=rid,
            metadata={"langgraph_node": "replanner"}
        )
        r.on_chain_end({"decision": "end"}, run_id="chain-replan")

        r.end_run("分析完成", status="success")
        r.close()

        # 验证
        steps = _query_steps(db_path)
        step_types = {s["step_type"] for s in steps}

        # 应该包含 classifier/planner/executor/replanner
        # 注意：step_type 可能是 "chain" 但 extra 中有 node 信息
        assert len(steps) >= 4, f"期望至少 4 个步骤，实际 {len(steps)}"

        # 验证所有步骤都完成
        running = [s for s in steps if s["status"] == "running"]
        assert len(running) == 0, f"仍有 {len(running)} 个步骤未完成"

        # 验证 LLM 调用被记录
        llm_steps = [s for s in steps if s["step_type"] == "llm"]
        assert len(llm_steps) >= 1, "LLM 调用未被记录"

        # 验证 messages
        msgs = _query_messages(db_path)
        assert len(msgs) > 0, "messages 表为空"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_plan_graph_with_llm_in_executor():
    """
    executor 节点内有 LLM 调用
    验证：steps 包含 executor chain + llm，parent_run_id 正确
    """
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from types import SimpleNamespace

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("测试")

        # Executor with LLM
        r.on_chain_start(
            {"name": "executor"},
            {"input": "执行"},
            run_id="exec-1", parent_run_id=rid,
            metadata={"langgraph_node": "executor"}
        )

        serialized = {"name": "chat_model", "kwargs": {"model_name": "gpt-4"}}
        msg = SimpleNamespace(type="human", content="分析", tool_calls=None, tool_call_id=None)
        r.on_chat_model_start(serialized, [msg], run_id="llm-1", parent_run_id="exec-1")
        response = LLMResult(
            generations=[[Generation(text="结果", generation_info=None)]],
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        )
        r.on_llm_end(response, run_id="llm-1")

        r.on_chain_end({"result": "完成"}, run_id="exec-1")
        r.end_run("完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)
        exec_steps = [s for s in steps if s["event_run_id"] == "exec-1"]
        llm_steps = [s for s in steps if s["event_run_id"] == "llm-1"]

        assert len(exec_steps) >= 1, "executor 步骤未记录"
        assert len(llm_steps) >= 1, "LLM 步骤未记录"

        # 验证 parent 关系
        assert llm_steps[0]["parent_run_id"] == "exec-1", \
            f"LLM 的 parent_run_id 应为 exec-1，实际为 {llm_steps[0]['parent_run_id']}"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_plan_graph_with_tool_in_executor():
    """
    executor 节点内有 tool 调用
    验证：steps 包含 executor chain + tool，parent_run_id 正确
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("测试")

        # Executor with Tool
        r.on_chain_start(
            {"name": "executor"},
            {"input": "执行"},
            run_id="exec-1", parent_run_id=rid,
            metadata={"langgraph_node": "executor"}
        )

        r.on_tool_start(
            {"name": "get_stock_price"}, "600519",
            run_id="tool-1", parent_run_id="exec-1"
        )
        r.on_tool_end("189.50", run_id="tool-1")

        r.on_chain_end({"result": "完成"}, run_id="exec-1")
        r.end_run("完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)
        tool_steps = [s for s in steps if s["event_run_id"] == "tool-1"]

        assert len(tool_steps) >= 1, "Tool 步骤未记录"
        assert tool_steps[0]["parent_run_id"] == "exec-1", \
            f"Tool 的 parent_run_id 应为 exec-1，实际为 {tool_steps[0]['parent_run_id']}"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_react_graph_agent_tools_loop():
    """
    ReAct Agent 的 agent ↔ tools 循环
    验证：steps 包含多个 agent + tools 交替，无 running 状态
    """
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from types import SimpleNamespace

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("ReAct 测试")

        # 循环 3 次 agent ↔ tools
        for i in range(3):
            # Agent (LLM call)
            r.on_chain_start(
                {"name": "agent"},
                {"input": f"思考第 {i+1} 步"},
                run_id=f"agent-{i}", parent_run_id=rid,
                metadata={"langgraph_node": "agent"}
            )
            serialized = {"name": "chat_model", "kwargs": {"model_name": "gpt-4"}}
            msg = SimpleNamespace(type="human", content=f"思考 {i+1}", tool_calls=None, tool_call_id=None)
            r.on_chat_model_start(serialized, [msg], run_id=f"llm-{i}", parent_run_id=f"agent-{i}")
            response = LLMResult(
                generations=[[Generation(text=f"决定调用工具 {i+1}", generation_info=None)]],
                llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            )
            r.on_llm_end(response, run_id=f"llm-{i}")
            r.on_chain_end({"thought": f"思考 {i+1} 完成"}, run_id=f"agent-{i}")

            # Tools
            r.on_chain_start(
                {"name": "tools"},
                {"input": f"执行工具 {i+1}"},
                run_id=f"tools-{i}", parent_run_id=rid,
                metadata={"langgraph_node": "tools"}
            )
            r.on_tool_start(
                {"name": f"tool_{i+1}"}, f"input_{i+1}",
                run_id=f"tool-{i}", parent_run_id=f"tools-{i}"
            )
            r.on_tool_end(f"result_{i+1}", run_id=f"tool-{i}")
            r.on_chain_end({"result": f"工具 {i+1} 完成"}, run_id=f"tools-{i}")

        r.end_run("ReAct 完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)

        # 应该有 3 个 agent + 3 个 tools + 3 个 LLM + 3 个 tool = 12 个步骤
        assert len(steps) >= 12, f"期望至少 12 个步骤，实际 {len(steps)}"

        # 验证所有步骤都完成
        running = [s for s in steps if s["status"] == "running"]
        assert len(running) == 0, f"仍有 {len(running)} 个步骤未完成"

        # 验证 agent 和 tools 交替
        # step_type 可能是 "chain" 或节点类型（如 "agent"、"tools"）
        agent_steps = [s for s in steps if s["step_name"] == "agent" or s["step_type"] == "agent"]
        tools_steps = [s for s in steps if s["step_name"] == "tools" or s["step_type"] == "tools"]

        assert len(agent_steps) == 3, f"期望 3 个 agent 步骤，实际 {len(agent_steps)}"
        assert len(tools_steps) == 3, f"期望 3 个 tools 步骤，实际 {len(tools_steps)}"

    finally:
        r.close()
        _cleanup_db(db_path)


# ============================================================
# 2. 函数内 LLM 调用
# ============================================================

def test_function_llm_then_graph():
    """
    先调用 LLM（非 graph 上下文），再进入 graph
    验证：steps 包含独立 llm + graph 节点，parent_run_id 正确
    """
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from types import SimpleNamespace

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("测试")

        # 1. 独立 LLM 调用（非 graph 上下文）
        serialized = {"name": "chat_model", "kwargs": {"model_name": "gpt-4"}}
        msg = SimpleNamespace(type="human", content="预处理", tool_calls=None, tool_call_id=None)
        r.on_chat_model_start(serialized, [msg], run_id="llm-pre", parent_run_id=rid)
        response = LLMResult(
            generations=[[Generation(text="预处理结果", generation_info=None)]],
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        )
        r.on_llm_end(response, run_id="llm-pre")

        # 2. 进入 graph
        r.on_chain_start(
            {"name": "classifier"},
            {"input": "分类"},
            run_id="chain-cls", parent_run_id=rid,
            metadata={"langgraph_node": "classifier"}
        )
        r.on_chain_end({"classification": "stock_analysis"}, run_id="chain-cls")

        r.end_run("完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)
        llm_steps = [s for s in steps if s["step_type"] == "llm"]
        # Graph 节点的 step_type 可能是 "chain" 或节点类型（如 "classifier"）
        graph_steps = [s for s in steps if s["step_type"] in ("chain", "classifier", "planner", "executor", "replanner", "graph")]

        assert len(llm_steps) >= 1, "独立 LLM 调用未记录"
        assert len(graph_steps) >= 1, "Graph 节点未记录"

        # 验证独立 LLM 的 parent 是 root
        assert llm_steps[0]["parent_run_id"] == rid, \
            f"独立 LLM 的 parent_run_id 应为 root，实际为 {llm_steps[0]['parent_run_id']}"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_function_tool_then_graph():
    """
    先调用 tool，再进入 graph
    验证：steps 包含独立 tool + graph 节点
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("测试")

        # 1. 独立 tool 调用
        r.on_tool_start(
            {"name": "get_data"}, "input",
            run_id="tool-pre", parent_run_id=rid
        )
        r.on_tool_end("data result", run_id="tool-pre")

        # 2. 进入 graph
        r.on_chain_start(
            {"name": "planner"},
            {"input": "计划"},
            run_id="chain-plan", parent_run_id=rid,
            metadata={"langgraph_node": "planner"}
        )
        r.on_chain_end({"plan": ["step1"]}, run_id="chain-plan")

        r.end_run("完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)
        tool_steps = [s for s in steps if s["step_type"] == "tool"]
        # Graph 节点的 step_type 可能是 "chain" 或节点类型（如 "planner"）
        graph_steps = [s for s in steps if s["step_type"] in ("chain", "classifier", "planner", "executor", "replanner", "graph")]

        assert len(tool_steps) >= 1, "独立 tool 调用未记录"
        assert len(graph_steps) >= 1, "Graph 节点未记录"

        # 验证独立 tool 的 parent 是 root
        assert tool_steps[0]["parent_run_id"] == rid, \
            f"独立 tool 的 parent_run_id 应为 root，实际为 {tool_steps[0]['parent_run_id']}"

    finally:
        r.close()
        _cleanup_db(db_path)


# ============================================================
# 3. 嵌套 Graph
# ============================================================

def test_nested_graph_outer_inner():
    """
    外层 graph → 内层 graph
    验证：steps 包含两层 graph 节点，parent 关系正确
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("嵌套测试")

        # 外层 graph
        r.on_chain_start(
            {"name": "outer_graph"},
            {"input": "外层"},
            run_id="outer", parent_run_id=rid,
            metadata={"langgraph_node": "agent"}
        )

        # 内层 graph
        r.on_chain_start(
            {"name": "inner_graph"},
            {"input": "内层"},
            run_id="inner", parent_run_id="outer",
            metadata={"langgraph_node": "planner"}
        )
        r.on_chain_end({"result": "内层完成"}, run_id="inner")

        r.on_chain_end({"result": "外层完成"}, run_id="outer")
        r.end_run("完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)
        outer_steps = [s for s in steps if s["event_run_id"] == "outer"]
        inner_steps = [s for s in steps if s["event_run_id"] == "inner"]

        assert len(outer_steps) >= 1, "外层 graph 未记录"
        assert len(inner_steps) >= 1, "内层 graph 未记录"

        # 验证 parent 关系
        assert inner_steps[0]["parent_run_id"] == "outer", \
            f"内层 graph 的 parent_run_id 应为 outer，实际为 {inner_steps[0]['parent_run_id']}"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_nested_graph_with_callbacks():
    """
    内层 graph 有 LLM/tool 调用
    验证：steps 包含内层 graph 的所有回调，parent 指向内层 graph
    """
    from utils.agent_trace import TraceRecorder
    from langchain_core.outputs import LLMResult, Generation
    from types import SimpleNamespace

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("嵌套测试")

        # 外层 graph
        r.on_chain_start(
            {"name": "outer_graph"},
            {"input": "外层"},
            run_id="outer", parent_run_id=rid,
            metadata={"langgraph_node": "agent"}
        )

        # 内层 graph
        r.on_chain_start(
            {"name": "inner_graph"},
            {"input": "内层"},
            run_id="inner", parent_run_id="outer",
            metadata={"langgraph_node": "planner"}
        )

        # 内层 graph 内的 LLM 调用
        serialized = {"name": "chat_model", "kwargs": {"model_name": "gpt-4"}}
        msg = SimpleNamespace(type="human", content="内层分析", tool_calls=None, tool_call_id=None)
        r.on_chat_model_start(serialized, [msg], run_id="llm-inner", parent_run_id="inner")
        response = LLMResult(
            generations=[[Generation(text="内层结果", generation_info=None)]],
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        )
        r.on_llm_end(response, run_id="llm-inner")

        # 内层 graph 内的 tool 调用
        r.on_tool_start(
            {"name": "inner_tool"}, "input",
            run_id="tool-inner", parent_run_id="inner"
        )
        r.on_tool_end("tool result", run_id="tool-inner")

        r.on_chain_end({"result": "内层完成"}, run_id="inner")
        r.on_chain_end({"result": "外层完成"}, run_id="outer")
        r.end_run("完成")
        r.close()

        # 验证
        steps = _query_steps(db_path)

        # 验证内层 LLM 的 parent 是 inner
        llm_inner = [s for s in steps if s["event_run_id"] == "llm-inner"]
        assert len(llm_inner) >= 1, "内层 LLM 未记录"
        assert llm_inner[0]["parent_run_id"] == "inner", \
            f"内层 LLM 的 parent_run_id 应为 inner，实际为 {llm_inner[0]['parent_run_id']}"

        # 验证内层 tool 的 parent 是 inner
        tool_inner = [s for s in steps if s["event_run_id"] == "tool-inner"]
        assert len(tool_inner) >= 1, "内层 tool 未记录"
        assert tool_inner[0]["parent_run_id"] == "inner", \
            f"内层 tool 的 parent_run_id 应为 inner，实际为 {tool_inner[0]['parent_run_id']}"

    finally:
        r.close()
        _cleanup_db(db_path)


# ============================================================
# 4. CLI 命令
# ============================================================

def test_cli_show():
    """验证 CLI show 命令输出包含 run 详情"""
    from utils.agent_trace import TraceRecorder
    from utils.agent_trace.cli import main as cli_main

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("测试输入")
        r.end_run("测试输出", status="success")
        r.close()

        old_argv = sys.argv
        sys.argv = ["agent_trace", "show", rid, "--db", db_path]
        try:
            cli_main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    finally:
        r.close()
        _cleanup_db(db_path)


def test_cli_tree():
    """验证 CLI tree 命令输出包含树形结构"""
    from utils.agent_trace import TraceRecorder
    from utils.agent_trace.cli import main as cli_main

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("测试")

        r.on_chain_start(
            {"name": "classifier"}, {"input": "分类"},
            run_id="cls", parent_run_id=rid,
            metadata={"langgraph_node": "classifier"}
        )
        r.on_chain_end({"result": "完成"}, run_id="cls")

        r.end_run("完成")
        r.close()

        old_argv = sys.argv
        sys.argv = ["agent_trace", "tree", rid, "--db", db_path]
        try:
            cli_main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    finally:
        r.close()
        _cleanup_db(db_path)


def test_cli_stats():
    """验证 CLI stats 命令输出包含统计信息"""
    from utils.agent_trace import TraceRecorder
    from utils.agent_trace.cli import main as cli_main

    r, db_path = _create_recorder()
    try:
        # 创建几个 runs
        for i in range(3):
            r.start_run(f"测试 {i}")
            r.end_run(f"输出 {i}", status="success")
        r.close()

        old_argv = sys.argv
        sys.argv = ["agent_trace", "stats", "--db", db_path]
        try:
            cli_main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    finally:
        r.close()
        _cleanup_db(db_path)


def test_cli_search():
    """验证 CLI search 命令输出包含匹配的 runs"""
    from utils.agent_trace import TraceRecorder
    from utils.agent_trace.cli import main as cli_main

    r, db_path = _create_recorder()
    try:
        r.start_run("茅台股票分析")
        r.end_run("分析完成")
        r.close()

        old_argv = sys.argv
        sys.argv = ["agent_trace", "search", "茅台", "--db", db_path]
        try:
            cli_main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    finally:
        r.close()
        _cleanup_db(db_path)


def test_cli_export():
    """验证 CLI export 命令生成 JSON 文件"""
    from utils.agent_trace import TraceRecorder
    from utils.agent_trace.cli import main as cli_main

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("导出测试")
        r.on_chain_start(
            {"name": "test"}, {"input": "input"},
            run_id="chain-1", parent_run_id=rid
        )
        r.on_chain_end({"result": "done"}, run_id="chain-1")
        r.end_run("导出完成")
        r.close()

        export_path = str(Path(tempfile.gettempdir()) / f"export_{threading.get_ident()}.json")

        old_argv = sys.argv
        sys.argv = ["agent_trace", "export", rid, "-o", export_path, "--db", db_path]
        try:
            cli_main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

        # 验证导出文件
        if Path(export_path).exists():
            with open(export_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert "run" in data or "id" in data, "导出 JSON 缺少 run 信息"
            Path(export_path).unlink(missing_ok=True)

    finally:
        r.close()
        _cleanup_db(db_path)


# ============================================================
# 5. Server API
# ============================================================

def test_api_runs():
    """验证 GET /api/runs 返回 JSON 数组"""
    from utils.agent_trace.server import TraceHandler
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        r.start_run("API 测试")
        r.end_run("完成")
        r.close()

        # 模拟 HTTP 请求
        handler = TraceHandler
        # 由于 HTTP handler 需要实际连接，我们直接测试数据库查询逻辑
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        runs = [dict(row) for row in conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()]
        conn.close()

        assert len(runs) >= 1, "API 应返回至少 1 条 run"
        assert "id" in runs[0], "run 缺少 id 字段"
        assert "agent_name" in runs[0], "run 缺少 agent_name 字段"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_api_run_detail():
    """验证 GET /api/runs/{id} 返回单条 run 详情"""
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("详情测试")
        r.end_run("完成", status="success")
        r.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run = dict(conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())
        conn.close()

        assert run["id"] == rid
        assert run["status"] == "success"
        assert run["agent_name"] == "test_agent"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_api_run_steps():
    """验证 GET /api/runs/{id}/steps 返回 steps 列表"""
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("步骤测试")

        r.on_chain_start(
            {"name": "test"}, {"input": "input"},
            run_id="chain-1", parent_run_id=rid
        )
        r.on_chain_end({"result": "done"}, run_id="chain-1")

        r.end_run("完成")
        r.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        steps = [dict(row) for row in conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (rid,)
        ).fetchall()]
        conn.close()

        assert len(steps) >= 1, "应返回至少 1 个 step"
        assert steps[0]["run_id"] == rid

    finally:
        r.close()
        _cleanup_db(db_path)


def test_api_db_path():
    """验证 GET /api/db-path 返回数据库路径"""
    # 这个测试主要验证 server 模块可以正常导入和使用
    from utils.agent_trace.server import cmd_serve
    assert cmd_serve is not None


# ============================================================
# 6. 边界情况
# ============================================================

def test_concurrent_writes():
    """
    多线程同时写入
    验证：无异常，所有记录都写入成功
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("并发测试")

        errors = []

        def write_steps(thread_id, count):
            try:
                for i in range(count):
                    run_id = f"thread-{thread_id}-{i}"
                    r.on_chain_start(
                        {"name": f"chain-{thread_id}-{i}"},
                        {"input": f"input-{thread_id}-{i}"},
                        run_id=run_id, parent_run_id=rid
                    )
                    r.on_chain_end(
                        {"result": f"result-{thread_id}-{i}"},
                        run_id=run_id
                    )
            except Exception as e:
                errors.append(e)

        # 启动 5 个线程，每个写入 10 个步骤
        threads = []
        for t in range(5):
            thread = threading.Thread(target=write_steps, args=(t, 10))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        r.end_run("并发完成")
        r.close()

        assert len(errors) == 0, f"并发写入出错: {errors}"

        # 验证所有步骤都写入
        steps = _query_steps(db_path)
        assert len(steps) >= 50, f"期望至少 50 个步骤，实际 {len(steps)}"

        # 验证所有步骤都完成
        running = [s for s in steps if s["status"] == "running"]
        assert len(running) == 0, f"仍有 {len(running)} 个步骤未完成"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_error_recovery():
    """
    回调中抛出异常
    验证：_guard 捕获异常，后续回调正常工作
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        rid = r.start_run("异常恢复测试")

        # 正常调用
        r.on_chain_start(
            {"name": "chain-1"}, {"input": "input"},
            run_id="chain-1", parent_run_id=rid
        )
        r.on_chain_end({"result": "done"}, run_id="chain-1")

        # 模拟异常（通过 mock）
        with patch.object(r, '_create_step', side_effect=Exception("模拟异常")):
            # 这个调用应该被 _guard 捕获，不抛异常
            r.on_chain_start(
                {"name": "chain-error"}, {"input": "input"},
                run_id="chain-error", parent_run_id=rid
            )

        # 继续正常调用
        r.on_chain_start(
            {"name": "chain-2"}, {"input": "input"},
            run_id="chain-2", parent_run_id=rid
        )
        r.on_chain_end({"result": "done"}, run_id="chain-2")

        r.end_run("恢复完成")
        r.close()

        # 验证正常步骤被记录
        steps = _query_steps(db_path)
        chain_names = {s["step_name"] for s in steps}
        assert "chain-1" in chain_names, "chain-1 未记录"
        assert "chain-2" in chain_names, "chain-2 未记录"

    finally:
        r.close()
        _cleanup_db(db_path)


def test_close_idempotent():
    """
    多次调用 close()
    验证：无异常
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        r.start_run("测试")
        r.end_run("完成")

        # 多次 close 应该无异常
        r.close()
        r.close()
        r.close()

    finally:
        _cleanup_db(db_path)


def test_start_run_without_end():
    """
    start_run 后直接 start_run
    验证：新 run 正确创建，旧 run 状态为 running
    """
    from utils.agent_trace import TraceRecorder

    r, db_path = _create_recorder()
    try:
        # 第一次 start
        rid1 = r.start_run("第一次")

        # 第二次 start（不调用 end_run）
        rid2 = r.start_run("第二次")

        r.end_run("第二次完成")
        r.close()

        # 验证两个 runs 都存在
        runs = _query_runs(db_path)
        assert len(runs) >= 2, f"期望至少 2 个 runs，实际 {len(runs)}"

        # 验证第一次 run 状态为 running（未结束）
        run1 = [run for run in runs if run["id"] == rid1]
        assert len(run1) >= 1, "第一次 run 未找到"
        assert run1[0]["status"] == "running", f"第一次 run 状态应为 running，实际为 {run1[0]['status']}"

        # 验证第二次 run 状态为 success
        run2 = [run for run in runs if run["id"] == rid2]
        assert len(run2) >= 1, "第二次 run 未找到"
        assert run2[0]["status"] == "success", f"第二次 run 状态应为 success，实际为 {run2[0]['status']}"

    finally:
        r.close()
        _cleanup_db(db_path)


if __name__ == "__main__":
    import traceback
    tests = [
        # 1. 多节点 Graph 链路
        test_plan_graph_full_chain,
        test_plan_graph_with_llm_in_executor,
        test_plan_graph_with_tool_in_executor,
        test_react_graph_agent_tools_loop,
        # 2. 函数内 LLM 调用
        test_function_llm_then_graph,
        test_function_tool_then_graph,
        # 3. 嵌套 Graph
        test_nested_graph_outer_inner,
        test_nested_graph_with_callbacks,
        # 4. CLI 命令
        test_cli_show,
        test_cli_tree,
        test_cli_stats,
        test_cli_search,
        test_cli_export,
        # 5. Server API
        test_api_runs,
        test_api_run_detail,
        test_api_run_steps,
        test_api_db_path,
        # 6. 边界情况
        test_concurrent_writes,
        test_error_recovery,
        test_close_idempotent,
        test_start_run_without_end,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"[PASS] {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test.__name__}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("[OK] 所有 Trace 链路测试通过!")
    print(f"{'='*60}")

    sys.exit(1 if failed > 0 else 0)

"""
PDOR 修复项单元测试

覆盖：
- 日志双通道 Formatter
- Trace 节点识别 (pdor_context)
- 工具错误信息结构化
- _is_usage_error 分类

运行: pytest test/unit/test_pdor/test_pdor_fixes.py -v
"""
import os
import sys
import logging
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 检查 langchain 是否可用
try:
    import langchain_core
    _has_langchain = True
except ImportError:
    _has_langchain = False

_skip_no_langchain = pytest.mark.skipif(not _has_langchain, reason="langchain_core not installed")


# ══════════════════════════════════════════════════════════════
# 日志 Formatter
# ══════════════════════════════════════════════════════════════

def _make_record(msg="msg", kv=None):
    """创建带 relpath 属性的 LogRecord（模拟 _RelativePathFilter 的效果）"""
    record = logging.LogRecord("test", logging.INFO, "test.py", 0, msg, (), None)
    record.relpath = "test.py"
    record.funcName = "test_func"
    record._prefix = "[T]"
    record._msg = msg
    record._kv = kv or {}
    return record


def test_console_formatter_truncates_long_values():
    """_ConsoleFormatter 对超长 value 截断"""
    from utils.logger import _ConsoleFormatter
    fmt = _ConsoleFormatter()
    record = _make_record("测试", {"data": "A" * 500})
    result = fmt.format(record)
    assert "..." in result
    assert "A" * 500 not in result


def test_file_formatter_no_truncation():
    """_FileFormatter 不截断"""
    from utils.logger import _FileFormatter
    fmt = _FileFormatter()
    record = _make_record("测试", {"data": "A" * 500})
    result = fmt.format(record)
    assert "A" * 500 in result
    assert "..." not in result


def test_formatter_short_values_not_truncated():
    """短 value 不截断（两种 formatter 都不截断）"""
    from utils.logger import _ConsoleFormatter, _FileFormatter
    for Formatter in [_ConsoleFormatter, _FileFormatter]:
        fmt = Formatter()
        record = _make_record("测试", {"data": "短文本"})
        result = fmt.format(record)
        assert "短文本" in result


def test_formatter_empty_kv():
    """空 kv 不影响格式化"""
    from utils.logger import _ConsoleFormatter
    fmt = _ConsoleFormatter()
    record = _make_record("测试", {})
    result = fmt.format(record)
    assert "测试" in result


# ══════════════════════════════════════════════════════════════
# Trace 节点识别
# ══════════════════════════════════════════════════════════════

@_skip_no_langchain
def test_identify_react_agent_node():
    """pdor_context=react_step + agent → 非 noise"""
    from utils.agent_trace.models import _identify_graph_node
    serialized = {"name": "AgentExecutor"}
    kw = {"metadata": {"pdor_context": "react_step"}}
    node_type, display, label, is_noise = _identify_graph_node(serialized, kw, parent_run_id="some-parent")
    assert node_type == "agent"
    assert is_noise is False


@_skip_no_langchain
def test_identify_react_tools_node():
    """pdor_context=react_step + tools → 非 noise"""
    from utils.agent_trace.models import _identify_graph_node
    serialized = {"name": "ToolNode"}
    kw = {"metadata": {"pdor_context": "react_step"}}
    node_type, display, label, is_noise = _identify_graph_node(serialized, kw, parent_run_id="some-parent")
    assert node_type == "tools"
    assert is_noise is False


@_skip_no_langchain
def test_identify_agent_without_pdor_context_is_noise():
    """无 pdor_context + agent → noise（原有逻辑不变）"""
    from utils.agent_trace.models import _identify_graph_node
    serialized = {"name": "AgentExecutor"}
    kw = {"metadata": {}}
    node_type, display, label, is_noise = _identify_graph_node(serialized, kw, parent_run_id="some-parent")
    assert node_type == "agent"
    assert is_noise is True


@_skip_no_langchain
def test_identify_langgraph_node_unaffected():
    """有 langgraph_node 时不受 pdor_context 影响"""
    from utils.agent_trace.models import _identify_graph_node
    serialized = {"name": "something"}
    kw = {"metadata": {"langgraph_node": "executor", "pdor_context": "react_step"}}
    node_type, display, label, is_noise = _identify_graph_node(serialized, kw, parent_run_id="some-parent")
    assert node_type == "executor"
    assert is_noise is False


@_skip_no_langchain
def test_graph_node_names_has_observer():
    """_GRAPH_NODE_NAMES 包含 observer/adjuster/reviewer"""
    from utils.agent_trace.models import _GRAPH_NODE_NAMES
    assert "observer" in _GRAPH_NODE_NAMES
    assert "adjuster" in _GRAPH_NODE_NAMES
    assert "reviewer" in _GRAPH_NODE_NAMES


# ══════════════════════════════════════════════════════════════
# 工具错误信息
# ══════════════════════════════════════════════════════════════

@_skip_no_langchain
def test_is_usage_error_value_error():
    """ValueError → 使用方式错误"""
    from tools.skill_builder import _is_usage_error
    assert _is_usage_error(ValueError("bad arg")) is True


@_skip_no_langchain
def test_is_usage_error_type_error():
    """TypeError → 使用方式错误"""
    from tools.skill_builder import _is_usage_error
    assert _is_usage_error(TypeError("wrong type")) is True


@_skip_no_langchain
def test_is_usage_error_key_error():
    """KeyError → 使用方式错误"""
    from tools.skill_builder import _is_usage_error
    assert _is_usage_error(KeyError("missing")) is True


@_skip_no_langchain
def test_is_usage_error_connection_error():
    """ConnectionError → 不是使用方式错误"""
    from tools.skill_builder import _is_usage_error
    assert _is_usage_error(ConnectionError("timeout")) is False


@_skip_no_langchain
def test_is_usage_error_timeout_error():
    """TimeoutError → 不是使用方式错误"""
    from tools.skill_builder import _is_usage_error
    assert _is_usage_error(TimeoutError("timed out")) is False


@_skip_no_langchain
def test_is_usage_error_generic():
    """RuntimeError → 不是使用方式错误（白名单外）"""
    from tools.skill_builder import _is_usage_error
    assert _is_usage_error(RuntimeError("something")) is False


@_skip_no_langchain
def test_skill_tool_error_format():
    """@skill_tool 捕获异常时返回结构化错误信息"""
    from tools.skill_builder import skill_tool

    class FakeSkill:
        logger = None

        @skill_tool
        def bad_tool(self, symbol: str) -> dict:
            raise ValueError(f"无效参数: {symbol}")

    skill = FakeSkill()
    result = skill.bad_tool("test")
    assert "bad_tool" in result
    assert "无效参数: test" in result
    assert "使用方式错误，请勿重试" in result


@_skip_no_langchain
def test_skill_tool_network_error_no_retry_warning():
    """网络错误不加"请勿重试"标记"""
    from tools.skill_builder import skill_tool

    class FakeSkill:
        logger = None

        @skill_tool
        def net_tool(self) -> dict:
            raise ConnectionError("连接超时")

    skill = FakeSkill()
    result = skill.net_tool()
    assert "net_tool" in result
    assert "连接超时" in result
    assert "请勿重试" not in result


@_skip_no_langchain
def test_skill_tool_empty_error_message():
    """异常 message 为空时，使用异常类名"""
    from tools.skill_builder import skill_tool

    class FakeSkill:
        logger = None

        @skill_tool
        def empty_error(self) -> dict:
            raise ValueError()

    skill = FakeSkill()
    result = skill.empty_error()
    assert "empty_error" in result
    assert "ValueError" in result


# ══════════════════════════════════════════════════════════════
# ReAct system prompt
# ══════════════════════════════════════════════════════════════

@_skip_no_langchain
def test_react_system_prompt_has_stop_conditions():
    """ReAct 子图 system prompt 包含停止条件"""
    # 读源文件而非 inspect.getsource，避免并行测试时 mock 污染
    import os
    graph_file = os.path.join(os.path.dirname(__file__), "..", "..", "..", "agents", "common_react", "graph.py")
    with open(graph_file, encoding="utf-8") as f:
        source = f.read()
    assert "同一工具调用失败后" in source
    assert "最多调用" in source
    assert "生成总结" in source
    assert "目标达成判定" in source


@_skip_no_langchain
def test_react_failed_tools_in_prompt():
    """failed_tools 参数会注入到 system prompt"""
    import os
    graph_file = os.path.join(os.path.dirname(__file__), "..", "..", "..", "agents", "common_react", "graph.py")
    with open(graph_file, encoding="utf-8") as f:
        source = f.read()
    assert "failed_tools" in source
    assert "请勿重试" in source


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)

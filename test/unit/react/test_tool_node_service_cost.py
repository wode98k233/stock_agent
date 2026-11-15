# -*- coding: utf-8 -*-
"""工具节点 → 按次计费成本回填（2026-10-18 新增）。

链路：provider 在响应里声明 `billed_service` → `run_web_search` / `fetch_web_page` 把它透传进
返回载荷 → 工具节点**在压缩之前**解析出来 → `compute_service_cost()` → 写进该 tool step 的
`extra.cost`。这里验证「解析 → 计价 → 回填」这一段真的接通了，且免费源不会被误记账。
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool


@pytest.fixture(autouse=True)
def _catalog(tmp_path, monkeypatch):
    """把运行时价目指到「仓库默认价目」的副本（含 services 段），避免受本机运行时价目影响。"""
    import config as config_module
    from utils import pricing as P
    default = P.default_catalog_path()
    path = Path(tmp_path) / "pricing_catalog.json"
    path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config_module.Config, "COST_PRICING_CATALOG_PATH", str(path), raising=False)
    P.invalidate()
    yield
    P.invalidate()


def _run(node, tool_name, args):
    async def go():
        return await node({
            "messages": [AIMessage(content="", tool_calls=[
                {"name": tool_name, "args": args, "id": "call-1"}])],
            "tool_calls_count": 0,
        })
    return asyncio.run(go())


def _build(tool_fn, tool_name, mock_logger):
    from agents.executor_callbacks import ExecutionState
    from agents.react.nodes import PerToolRateLimiter, ToolNode

    exec_state = ExecutionState()
    arg_str = str({"query": "A"}) if tool_name == "web_search" else str({"url": "A"})
    exec_state.start_tool(tool_name, arg_str, tool_call_id="run-1")
    exec_state.end_tool("", tool_call_id="run-1")
    node = ToolNode(
        exec_state=exec_state,
        tools=[tool_fn],
        logger=mock_logger,
        rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
    )
    return node, arg_str


def test_billed_search_writes_service_cost(mock_logger):
    payload = json.dumps({"success": True, "provider": "DeepSeek",
                          "billed_service": "deepseek-search", "results": [{"title": "t"}]},
                         ensure_ascii=False)

    @tool
    async def web_search(query: str) -> str:
        """假的通用网搜工具。"""
        return payload

    node, _ = _build(web_search, "web_search", mock_logger)
    recorder = MagicMock()
    with patch("utils.agent_trace.core.get_active_recorder", return_value=recorder):
        _run(node, "web_search", {"query": "A"})

    assert recorder.update_tool_step_cost.called
    run_id, cost = recorder.update_tool_step_cost.call_args.args
    assert run_id == "run-1"
    assert cost["kind"] == "service" and cost["service"] == "deepseek-search"
    assert cost["total"] == pytest.approx(0.005)
    assert cost["price_snapshot"]["price"] == pytest.approx(0.005)


def test_billed_fetch_writes_service_cost(mock_logger):
    payload = json.dumps({"success": True, "url": "https://x/y", "content": "正文",
                          "billed": True, "billed_service": "deepseek-fetch"})

    @tool
    async def web_fetch(url: str) -> str:
        """假的抓取工具。"""
        return payload

    node, _ = _build(web_fetch, "web_fetch", mock_logger)
    recorder = MagicMock()
    with patch("utils.agent_trace.core.get_active_recorder", return_value=recorder):
        _run(node, "web_fetch", {"url": "A"})

    _, cost = recorder.update_tool_step_cost.call_args.args
    assert cost["service"] == "deepseek-fetch"


def test_free_source_records_nothing(mock_logger):
    """命中 MX 等免费源时不得记账（它确实不花钱）"""
    payload = json.dumps({"success": True, "provider": "MX", "results": [{"title": "t"}]})

    @tool
    async def web_search(query: str) -> str:
        """假的通用网搜工具。"""
        return payload

    node, _ = _build(web_search, "web_search", mock_logger)
    recorder = MagicMock()
    with patch("utils.agent_trace.core.get_active_recorder", return_value=recorder):
        _run(node, "web_search", {"query": "A"})

    assert not recorder.update_tool_step_cost.called


def test_cached_fetch_records_nothing(mock_logger):
    """缓存命中（billed 归零）时不得重复记账"""
    payload = json.dumps({"success": True, "billed": False, "billed_service": "",
                          "cached": True, "content": "正文"})

    @tool
    async def web_fetch(url: str) -> str:
        """假的抓取工具。"""
        return payload

    node, _ = _build(web_fetch, "web_fetch", mock_logger)
    recorder = MagicMock()
    with patch("utils.agent_trace.core.get_active_recorder", return_value=recorder):
        _run(node, "web_fetch", {"url": "A"})

    assert not recorder.update_tool_step_cost.called


def test_no_recorder_is_silent(mock_logger):
    """没开 trace 时不得因为记账失败影响工具输出"""
    payload = json.dumps({"billed_service": "deepseek-search", "results": [{"title": "t"}]})

    @tool
    async def web_search(query: str) -> str:
        """假的通用网搜工具。"""
        return payload

    node, _ = _build(web_search, "web_search", mock_logger)
    with patch("utils.agent_trace.core.get_active_recorder", return_value=None):
        result = _run(node, "web_search", {"query": "A"})
    assert "billed_service" in result["messages"][0].content


def test_cost_switch_off_records_nothing(mock_logger, monkeypatch):
    import config as config_module
    monkeypatch.setattr(config_module.Config, "COST_TRACKING_ENABLED", False, raising=False)
    payload = json.dumps({"billed_service": "deepseek-search", "results": [{"title": "t"}]})

    @tool
    async def web_search(query: str) -> str:
        """假的通用网搜工具。"""
        return payload

    node, _ = _build(web_search, "web_search", mock_logger)
    recorder = MagicMock()
    with patch("utils.agent_trace.core.get_active_recorder", return_value=recorder):
        _run(node, "web_search", {"query": "A"})
    assert not recorder.update_tool_step_cost.called

"""`tools/search/mcp_client.py` 单元测试（2026-10-07 新增）。

免 Key 托管搜索 MCP（Parallel / Exa）走的是 MCP streamable-HTTP：
`initialize` → `notifications/initialized` → `tools/call`（SSE 返回）。

本测试**完全不发网络请求**（patch `requests`），锁住：
1. `initialize` 必须捕获并复用 `mcp-session-id` 响应头；
2. SSE 解析要按 **UTF-8**（requests 的 `.text` 无 charset 头时会猜 Latin-1，中文乱码）；
3. JSON-RPC `error` → `MCPError`；工具 `isError` → `MCPError`；
4. 会话失效（400/404）→ 重握手并**只重试一次**；
5. 网络异常 → `MCPError`（provider 层不抛到聚合器）。
"""
import json

import pytest

from tools.search import mcp_client
from tools.search.mcp_client import MCPError, MCPHttpClient


class _Resp:
    def __init__(self, status_code=200, content=b"", headers=None, text=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text if text is not None else content.decode("utf-8", "ignore")


class _FakeRequests:
    """按调用次序返回预置响应的假 requests 模块。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "payload": json, "headers": headers or {}, "timeout": timeout})
        if not self._responses:
            raise AssertionError("收到超出预期的请求")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def exceptions(self):
        import requests
        return requests.exceptions


def _sse(obj) -> bytes:
    return f"event: message\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _init_resp(session="sess-123"):
    return _Resp(200, _sse({"jsonrpc": "2.0", "id": 1,
                            "result": {"serverInfo": {"name": "T", "version": "1"}}}),
                 headers={"mcp-session-id": session})


def _ok_notification():
    return _Resp(202, b"")


def _tool_resp(text, is_error=False):
    return _Resp(200, _sse({"jsonrpc": "2.0", "id": 3,
                            "result": {"content": [{"type": "text", "text": text}],
                                       "isError": is_error}}))


@pytest.fixture
def fake(monkeypatch):
    def _install(responses):
        fr = _FakeRequests(responses)
        monkeypatch.setattr(mcp_client, "requests", fr)
        return fr
    return _install


# ── ① 会话握手 ──────────────────────────────────────────────

def test_initialize_captures_session_id(fake):
    fr = fake([_init_resp("sess-abc"), _ok_notification(), _tool_resp("ok")])
    client = MCPHttpClient("https://x/mcp")
    assert client.call_tool("t", {}) == "ok"

    # 第一次 initialize 不带会话头；notifications 与 tools/call 必须带
    assert "Mcp-Session-Id" not in fr.calls[0]["headers"]
    assert fr.calls[1]["headers"]["Mcp-Session-Id"] == "sess-abc"
    assert fr.calls[2]["headers"]["Mcp-Session-Id"] == "sess-abc"
    assert client.server_info == {"name": "T", "version": "1"}


def test_headers_include_mcp_required_accept(fake):
    fr = fake([_init_resp(), _ok_notification(), _tool_resp("ok")])
    MCPHttpClient("https://x/mcp").call_tool("t", {})
    h = fr.calls[0]["headers"]
    assert "application/json" in h["Content-Type"]
    assert "text/event-stream" in h["Accept"]


def test_session_is_reused_across_calls(fake):
    """第二次调用不应再握手（省 2 个往返）。"""
    fr = fake([_init_resp(), _ok_notification(), _tool_resp("a"), _tool_resp("b")])
    client = MCPHttpClient("https://x/mcp")
    client.call_tool("t", {})
    client.call_tool("t", {})
    methods = [c["payload"].get("method") for c in fr.calls]
    assert methods.count("initialize") == 1


# ── ② SSE 解析（UTF-8 / 形态兼容）────────────────────────────

def test_sse_utf8_chinese_not_mojibake(fake):
    """回归：requests.text 无 charset 头时猜 Latin-1，中文会乱码；必须按 UTF-8 解。"""
    fake([_init_resp(), _ok_notification(), _tool_resp("贵州茅台 最新消息")])
    assert MCPHttpClient("https://x/mcp").call_tool("t", {}) == "贵州茅台 最新消息"


def test_plain_json_response_supported(fake):
    """非 SSE（直接 JSON）也要能解析。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 3,
                       "result": {"content": [{"type": "text", "text": "plain"}]}},
                      ensure_ascii=False).encode("utf-8")
    fake([_init_resp(), _ok_notification(), _Resp(200, body)])
    assert MCPHttpClient("https://x/mcp").call_tool("t", {}) == "plain"


def test_multiple_data_events_picks_result(fake):
    """同一响应里若先有通知后有结果，应取带 result 的那条。"""
    raw = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n' \
          b'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"hit"}]}}\n\n'
    fake([_init_resp(), _ok_notification(), _Resp(200, raw)])
    assert MCPHttpClient("https://x/mcp").call_tool("t", {}) == "hit"


# ── ③ 错误透传 ──────────────────────────────────────────────

def test_tool_iserror_raises(fake):
    fake([_init_resp(), _ok_notification(), _tool_resp("rate limit exceeded", is_error=True)])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "rate limit" in str(ei.value)


def test_jsonrpc_error_raises(fake):
    err = _Resp(200, _sse({"jsonrpc": "2.0", "id": 3,
                           "error": {"code": -32602, "message": "invalid params"}}))
    fake([_init_resp(), _ok_notification(), err])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "invalid params" in str(ei.value)


def test_initialize_http_error_raises(fake):
    fake([_Resp(500, b"boom")])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "initialize 失败" in str(ei.value)


def test_tools_call_http_error_raises(fake):
    fake([_init_resp(), _ok_notification(), _Resp(403, b"forbidden")])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "403" in str(ei.value)


def test_network_exception_becomes_mcp_error(fake):
    import requests
    fake([requests.exceptions.ConnectionError("dns fail")])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "网络请求失败" in str(ei.value)


def test_timeout_becomes_mcp_error(fake):
    import requests
    fake([_init_resp(), _ok_notification(), requests.exceptions.Timeout()])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "超时" in str(ei.value)


# ── ④ 会话失效重试 ──────────────────────────────────────────

def test_expired_session_reinitializes_and_retries_once(fake):
    fr = fake([
        _init_resp("old"), _ok_notification(),
        _Resp(404, b"session not found"),      # 首次 tools/call 会话失效
        _init_resp("new"), _ok_notification(),
        _tool_resp("recovered"),               # 重试成功
    ])
    client = MCPHttpClient("https://x/mcp")
    assert client.call_tool("t", {}) == "recovered"
    assert [c["payload"].get("method") for c in fr.calls].count("initialize") == 2


def test_expired_session_retries_only_once(fake):
    """连续 404 不得无限重试（只重试一次，第二次失败即抛）。"""
    fake([
        _init_resp("old"), _ok_notification(),
        _Resp(404, b"gone"),
        _init_resp("new"), _ok_notification(),
        _Resp(404, b"gone again"),
    ])
    with pytest.raises(MCPError) as ei:
        MCPHttpClient("https://x/mcp").call_tool("t", {})
    assert "404" in str(ei.value)


# ── ⑤ list_tools ────────────────────────────────────────────

def test_list_tools(fake):
    body = _sse({"jsonrpc": "2.0", "id": 2,
                 "result": {"tools": [{"name": "web_search", "inputSchema": {}}]}})
    fake([_init_resp(), _ok_notification(), _Resp(200, body)])
    tools = MCPHttpClient("https://x/mcp").list_tools()
    assert [t["name"] for t in tools] == ["web_search"]


def test_empty_body_does_not_crash(fake):
    """空响应体（某些实现 tools/call 返回 202）不得抛未捕获异常。"""
    fake([_init_resp(), _ok_notification(), _Resp(200, b"")])
    with pytest.raises(MCPError):
        MCPHttpClient("https://x/mcp").call_tool("t", {})

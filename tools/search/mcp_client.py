# -*- coding: utf-8 -*-
"""最小 MCP (Model Context Protocol) streamable-HTTP 客户端

## 为什么自己写（2026-10-07）

Parallel / Exa 提供**免 Key** 的托管搜索 MCP（`search.parallel.ai/mcp`、`mcp.exa.ai/mcp`），
国内网络实测可达（DDG 系全部超时，见 `memory/2026-10-06.md`）。但接入它们**不需要**
引入 `mcp` SDK —— 协议本质就是 3 个普通 POST：

1. `initialize`                      → 200，响应头带 `mcp-session-id`
2. `notifications/initialized`       （带 `Mcp-Session-Id`，无响应体）
3. `tools/call`                      → SSE：`event: message` + `data: {json-rpc}`

必需请求头：`Content-Type: application/json` + `Accept: application/json, text/event-stream`。
用一个 30 行的客户端把协议细节封在这里，provider 侧只关心「工具名 + 参数 → 文本结果」。

## 会话复用

`mcp-session-id` 有 TTL。这里**缓存会话**避免每次搜索都花 2 个额外往返；一旦
`tools/call` 因会话失效返回 400/404，就清空会话**重握手并重试一次**（只一次，防死循环）。
"""
import json
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("radar.search")


class MCPError(Exception):
    """MCP 调用失败（网络 / 协议 / 工具返回 isError）。"""


class MCPHttpClient:
    """MCP streamable-HTTP 最小客户端。

    用法::

        client = MCPHttpClient("https://search.parallel.ai/mcp")
        text = client.call_tool("web_search", {"objective": "...", "search_queries": [...]})
    """

    DEFAULT_TIMEOUT = 20
    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, endpoint: str, timeout: int = DEFAULT_TIMEOUT,
                 user_agent: str = "stock-radar/1.0 (+mcp-http)"):
        self.endpoint = endpoint
        self.timeout = timeout
        self._user_agent = user_agent
        self._session_id: Optional[str] = None
        self._server_info: Optional[dict] = None

    # ── 内部：请求头 ────────────────────────────────────────────

    def _headers(self, with_session: bool = False) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self._user_agent,
        }
        if with_session and self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    # ── 内部：SSE / JSON 响应解析 ───────────────────────────────

    @staticmethod
    def _parse_payload(resp) -> dict:
        """从响应里取出 JSON-RPC 消息。

        兼容三种形态：① SSE（`event: message` + 一条或多条 `data:`）；
        ② 纯 JSON；③ 空体（notifications 的 202）。
        多个 data 事件时取**最后一个带 result/error 的**（前面的可能是通知）。
        """
        # 显式按 UTF-8 解码：requests 的 .text 在无 charset 头时会猜成 Latin-1，
        # 中文会变乱码（实测 Exa 的 SSE 就踩了这个）。
        raw = resp.content.decode("utf-8", errors="ignore") if resp.content else ""
        if not raw.strip():
            return {}

        found: dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                msg = json.loads(chunk)
            except Exception:
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                found = msg
        if found:
            return found

        # 非 SSE：整段当 JSON 解析
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _post(self, payload: dict, with_session: bool, timeout: int):
        """POST 并把网络异常统一翻译成 `MCPError`。

        放在这里而不是各调用点：`initialize` 阶段的网络失败若逃逸成
        `requests.exceptions.ConnectionError`，调用方就得同时 catch 两种异常类型
        （2026-10-07 单测 `test_network_exception_becomes_mcp_error` 抓到的真实缺口）。
        现在 provider 侧只需处理 `MCPError` 一种。
        """
        try:
            return requests.post(self.endpoint, json=payload,
                                 headers=self._headers(with_session), timeout=timeout)
        except requests.exceptions.Timeout:
            raise MCPError(f"请求超时（{timeout}s）")
        except requests.exceptions.RequestException as e:
            raise MCPError(f"网络请求失败: {e}")

    @staticmethod
    def _raise_for_error(msg: dict) -> None:
        err = msg.get("error")
        if err:
            if isinstance(err, dict):
                raise MCPError(f"MCP 错误 {err.get('code')}: {err.get('message')}")
            raise MCPError(f"MCP 错误: {err}")

    # ── 会话握手 ────────────────────────────────────────────────

    def _initialize(self, timeout: int) -> None:
        """握手：initialize + notifications/initialized，缓存 session id。"""
        resp = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stock-radar", "version": "1.0"},
            },
        }, with_session=False, timeout=timeout)

        if resp.status_code not in (200, 201):
            raise MCPError(f"initialize 失败: HTTP {resp.status_code} {resp.text[:160]}")

        self._session_id = resp.headers.get("mcp-session-id") or self._session_id
        msg = self._parse_payload(resp)
        self._raise_for_error(msg)
        self._server_info = (msg.get("result") or {}).get("serverInfo") or self._server_info

        # notifications/initialized —— 协议要求的收尾通知，失败不影响后续调用
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                       with_session=True, timeout=timeout)
        except Exception as e:
            logger.debug(f"[MCP] initialized 通知失败（忽略）: {e}")

    @property
    def server_info(self) -> Optional[dict]:
        return self._server_info

    def list_tools(self, timeout: Optional[int] = None) -> List[dict]:
        """列出工具（诊断/测试用）。"""
        t = timeout or self.timeout
        if not self._session_id:
            self._initialize(t)
        resp = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                          with_session=True, timeout=t)
        if resp.status_code != 200:
            raise MCPError(f"tools/list 失败: HTTP {resp.status_code}")
        msg = self._parse_payload(resp)
        self._raise_for_error(msg)
        return (msg.get("result") or {}).get("tools") or []

    # ── 工具调用 ────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: Dict[str, Any],
                  timeout: Optional[int] = None) -> str:
        """调用 MCP 工具，返回拼接后的文本结果。

        Raises:
            MCPError: 网络异常 / HTTP 非 200 / JSON-RPC error / 工具 isError。
        """
        t = timeout or self.timeout
        if not self._session_id:
            self._initialize(t)

        resp = self._call_once(tool_name, arguments, t)
        # 会话可能已过期（TTL 到期）→ 重握手并重试一次
        if resp.status_code in (400, 404) and self._session_id:
            logger.info("[MCP] 会话失效，重新握手后重试一次")
            self._session_id = None
            self._initialize(t)
            resp = self._call_once(tool_name, arguments, t)

        if resp.status_code != 200:
            raise MCPError(f"tools/call 失败: HTTP {resp.status_code} {resp.text[:160]}")

        msg = self._parse_payload(resp)
        self._raise_for_error(msg)
        result = msg.get("result")
        if result is None:
            # 协议要求 tools/call 必带 result；空体/不可解析属于异常，必须显式报错，
            # 否则会退化成「text=''」→ 上层只报「未返回可解析结果」，丢失真因。
            raise MCPError(f"tools/call 返回体无法解析: HTTP {resp.status_code} {resp.text[:160]}")

        text = "".join(c.get("text", "") for c in (result.get("content") or [])
                       if isinstance(c, dict))
        if result.get("isError"):
            raise MCPError(f"工具 {tool_name} 返回错误: {text[:200]}")
        return text

    def _call_once(self, tool_name: str, arguments: Dict[str, Any], timeout: int):
        return self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }, with_session=True, timeout=timeout)

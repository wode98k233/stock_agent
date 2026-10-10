# -*- coding: utf-8 -*-
"""Exa 搜索引擎 Provider（免 Key，经 MCP）

特点：
- 托管 MCP：`https://mcp.exa.ai/mcp`，工具 `web_search_exa`
- **免 API Key**、**国内网络实测可达**（2026-10-07 实测 1.6s 返回真实结果）
- 返回面向 LLM 的干净正文/Highlights
- 出站到境外服务，合规需自行判断；可用 `SEARCH_FREE_MCP_ENABLED=false` 整体关闭

⚠️ 免 Key 为厂商免费档，有频率限制，非承诺 SLA。

工具参数（inputSchema 实测）：
- `query`（必填）、`objective`（必填）、`numResults`（可选）

返回形态（实测，**不是 JSON**，是分块 Markdown）：

    Title: 公司公告
    URL: https://example.com/a
    Published: 2026-10-06T00:00:00.000Z
    Author: N/A
    Highlights:
    - 要点一
    - 要点二

    ---

    Title: 下一条
    ...
"""
import logging
import re
from typing import List

from .base import BaseSearchProvider, SearchResponse, SearchResult
from .mcp_client import MCPError, MCPHttpClient

logger = logging.getLogger("radar.search")

_BLOCK_SEP = re.compile(r"\n\s*-{3,}\s*\n")
_HEADER_KEYS = ("Title:", "URL:", "Published:", "Author:", "Highlights:",
                "Text:", "Summary:", "Score:", "ID:")


class ExaSearchProvider(BaseSearchProvider):
    """Exa Search（MCP，免 Key）"""

    name = "Exa"
    ENDPOINT = "https://mcp.exa.ai/mcp"
    TOOL = "web_search_exa"
    DEFAULT_TIMEOUT = 15

    # 免密：托管 MCP 无 Key，必须显式声明（否则被基类「未配置 API Key」静默短路）
    requires_api_key = False

    def __init__(self, api_keys: List[str] = None):
        super().__init__(api_keys)
        self._client = None

    @property
    def is_available(self) -> bool:
        return _free_mcp_enabled()

    def _get_client(self) -> MCPHttpClient:
        if self._client is None:
            self._client = MCPHttpClient(self.ENDPOINT, timeout=self.DEFAULT_TIMEOUT)
        return self._client

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        try:
            text = self._get_client().call_tool(self.TOOL, {
                "query": query,
                "objective": f"检索与「{query}」相关的最新公开信息",
                "numResults": max_results,
            }, timeout=self.DEFAULT_TIMEOUT)
        except MCPError as e:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=f"Exa MCP: {e}")
        except Exception as e:  # noqa: BLE001
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=f"Exa 调用失败: {e}")

        results = _parse_exa_payload(text, max_results)
        if not results:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message="Exa 未返回可解析结果")
        return SearchResponse(query=query, results=results, provider=self.name, success=True)


def _free_mcp_enabled() -> bool:
    """免 Key 境外 MCP 源的总开关（默认开启）。"""
    import os
    return os.getenv("SEARCH_FREE_MCP_ENABLED", "true").strip().lower() not in ("false", "0", "no")


def _parse_exa_payload(text: str, max_results: int) -> List[SearchResult]:
    """把 Exa 的分块 Markdown 解析成 SearchResult 列表。"""
    if not text or not text.strip():
        return []

    results: List[SearchResult] = []
    for block in _BLOCK_SEP.split(text):
        block = block.strip()
        if not block:
            continue

        m_url = re.search(r"^URL:\s*(\S+)", block, re.M)
        if not m_url:
            continue
        url = m_url.group(1).strip()

        m_title = re.search(r"^Title:\s*(.+)$", block, re.M)
        m_pub = re.search(r"^Published:\s*(.+)$", block, re.M)

        body = "\n".join(
            ln for ln in block.splitlines()
            if not ln.strip().startswith(_HEADER_KEYS)
        ).strip()

        results.append(SearchResult(
            title=(m_title.group(1).strip() if m_title else ""),
            snippet=body[:2000],
            url=url,
            source=BaseSearchProvider._extract_domain(url),
            published_date=_clean_date(m_pub.group(1) if m_pub else None),
        ))
        if len(results) >= max_results:
            break
    return results


def _clean_date(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.upper() in ("N/A", "NONE", "NULL"):
        return ""
    # 取 ISO 日期的日期部分（Exa 给的是 2026-10-06T00:00:00.000Z）
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s

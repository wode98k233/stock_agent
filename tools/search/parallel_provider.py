# -*- coding: utf-8 -*-
"""Parallel Search 搜索引擎 Provider（免 Key，经 MCP）

特点：
- 托管 MCP：`https://search.parallel.ai/mcp`，工具 `web_search`
- **免 API Key**、**国内网络实测可达**（2026-10-07 实测 1.6s 返回真实中文财经资讯）
- 返回 excerpt（正文摘录），信息密度高于普通搜索摘要
- 出站到境外服务，合规需自行判断；可用 `SEARCH_FREE_MCP_ENABLED=false` 整体关闭

⚠️ 免 Key 是厂商免费档，有频率限制，非承诺 SLA；超限会报错，此时按失败处理即可
（聚合器会继续尝试下一个源）。

工具参数（inputSchema 实测）：
- `objective`（必填）：本次搜索的目标描述，用于提升召回
- `search_queries`（必填）：查询词列表
- `session_id` / `model_name`（可选）

返回 JSON（实测）：
`{"search_id": ..., "results": [{"url","title","publish_date","excerpts":[...]}]}`
"""
import json
import logging
import os
from typing import List

from .base import BaseSearchProvider, SearchResponse, SearchResult
from .mcp_client import MCPError, MCPHttpClient

logger = logging.getLogger("radar.search")


class ParallelSearchProvider(BaseSearchProvider):
    """Parallel Web Search（MCP，免 Key）"""

    name = "Parallel"
    ENDPOINT = "https://search.parallel.ai/mcp"
    TOOL = "web_search"
    DEFAULT_TIMEOUT = 15

    # 免密：凭据不通过 api_keys 传（托管 MCP 无 Key），必须显式声明，
    # 否则基类 `search()` 会在「未配置 API Key」处静默短路（见 base.py 注释）
    requires_api_key = False

    def __init__(self, api_keys: List[str] = None):
        super().__init__(api_keys)
        self._client = None

    @property
    def is_available(self) -> bool:
        """默认可用；`SEARCH_FREE_MCP_ENABLED=false` 时整体关闭（合规开关）。"""
        return _free_mcp_enabled()

    def _get_client(self) -> MCPHttpClient:
        """延迟构造（不在 import/init 阶段发网络请求）。"""
        if self._client is None:
            self._client = MCPHttpClient(self.ENDPOINT, timeout=self.DEFAULT_TIMEOUT)
        return self._client

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 Parallel 搜索。"""
        try:
            text = self._get_client().call_tool(self.TOOL, {
                "objective": f"检索与「{query}」相关的最新公开信息",
                "search_queries": [query],
            }, timeout=self.DEFAULT_TIMEOUT)
        except MCPError as e:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=f"Parallel MCP: {e}")
        except Exception as e:  # noqa: BLE001 —— provider 层不得抛到聚合器
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=f"Parallel 调用失败: {e}")

        results = _parse_parallel_payload(text, max_results)
        if not results:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message="Parallel 未返回可解析结果")
        return SearchResponse(query=query, results=results, provider=self.name, success=True)


def _free_mcp_enabled() -> bool:
    """免 Key 境外 MCP 源的总开关（默认开启）。"""
    return os.getenv("SEARCH_FREE_MCP_ENABLED", "true").strip().lower() not in ("false", "0", "no")


def _parse_parallel_payload(text: str, max_results: int) -> List[SearchResult]:
    """把 Parallel 的 JSON 文本解析成 SearchResult 列表。

    容错：`excerpts` 可能是 list 或 str；`publish_date` 可能为 null / "N/A"。
    """
    try:
        data = json.loads(text)
    except Exception:
        logger.warning("[Parallel] 返回非 JSON，无法解析")
        return []

    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    results: List[SearchResult] = []
    for item in items[:max_results]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        excerpts = item.get("excerpts")
        if isinstance(excerpts, list):
            snippet = "\n".join(str(x) for x in excerpts if x)
        else:
            snippet = str(excerpts or "")
        results.append(SearchResult(
            title=str(item.get("title") or "").strip(),
            snippet=snippet[:2000],
            url=url,
            source=BaseSearchProvider._extract_domain(url),
            published_date=_clean_date(item.get("publish_date")),
        ))
    return results


def _clean_date(value) -> str:
    """归一化日期：None / "N/A" / "" → 空串。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.upper() in ("N/A", "NONE", "NULL") else s

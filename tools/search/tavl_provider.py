# -*- coding: utf-8 -*-
"""Tavily 搜索引擎 Provider

特点：
- 专为 AI/LLM 优化的搜索 API
- 免费版每月 1000 次请求
- 返回结构化的搜索结果

文档：https://docs.tavily.com/
"""
import logging
from typing import List

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class TavilySearchProvider(BaseSearchProvider):
    """Tavily 搜索引擎"""

    name = "Tavily"

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 Tavily 搜索"""
        try:
            from tavily import TavilyClient
        except ImportError:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="tavily-python 未安装"
            )

        days = kwargs.get("days", 7)

        try:
            client = TavilyClient(api_key=api_key)
            response = client.search(
                query=query,
                search_depth="advanced",
                max_results=max_results,
                include_answer=False,
                include_raw_content=False,
                days=days,
            )

            results = []
            for item in response.get('results', []):
                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=item.get('content', '')[:500],
                    url=item.get('url', ''),
                    source=self._extract_domain(item.get('url', '')),
                    published_date=item.get('published_date') or item.get('publishedDate'),
                ))

            return SearchResponse(
                query=query, results=results, provider=self.name, success=True
            )

        except Exception as e:
            error_msg = str(e)
            if 'rate limit' in error_msg.lower() or 'quota' in error_msg.lower():
                error_msg = f"API 配额已用尽: {error_msg}"
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=error_msg
            )

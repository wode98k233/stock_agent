# -*- coding: utf-8 -*-
"""SerpAPI 搜索引擎 Provider

特点：
- 支持 Google、Bing、百度等多种搜索引擎
- 免费版每月 100 次请求
- 返回真实的搜索结果

文档：https://serpapi.com/
"""
import logging
from typing import List

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class SerpAPISearchProvider(BaseSearchProvider):
    """SerpAPI 搜索引擎"""

    name = "SerpAPI"
    API_ENDPOINT = "https://serpapi.com/search"

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 SerpAPI 搜索"""
        days = kwargs.get("days", 7)

        # 时间范围映射
        if days <= 1:
            tbs = "qdr:d"  # 过去24小时
        elif days <= 7:
            tbs = "qdr:w"  # 过去一周
        elif days <= 30:
            tbs = "qdr:m"  # 过去一月
        else:
            tbs = "qdr:y"  # 过去一年

        params = {
            "q": query,
            "api_key": api_key,
            "engine": "google",
            "google_domain": "google.com.hk",
            "hl": "zh-cn",
            "gl": "cn",
            "tbs": tbs,
            "num": min(max_results, 10)
        }

        try:
            response = requests.get(self.API_ENDPOINT, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            results = []

            # 解析知识图谱
            kg = data.get('knowledge_graph', {})
            if kg:
                results.append(SearchResult(
                    title=f"[知识图谱] {kg.get('title', '')}",
                    snippet=kg.get('description', ''),
                    url=kg.get('source', {}).get('link', ''),
                    source="Google Knowledge Graph"
                ))

            # 解析精选回答
            ab = data.get('answer_box', {})
            if ab:
                snippet = ab.get('snippet') or ab.get('answer', '')
                if snippet:
                    results.append(SearchResult(
                        title=f"[精选回答] {ab.get('title', '')}",
                        snippet=snippet[:500],
                        url=ab.get('link', ''),
                        source="Google Answer Box"
                    ))

            # 解析自然搜索结果
            for item in data.get('organic_results', [])[:max_results]:
                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=item.get('snippet', '')[:500],
                    url=item.get('link', ''),
                    source=self._extract_domain(item.get('link', '')),
                    published_date=item.get('date')
                ))

            return SearchResponse(
                query=query, results=results, provider=self.name, success=True
            )

        except requests.exceptions.Timeout:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="请求超时"
            )
        except requests.exceptions.RequestException as e:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=f"网络请求失败: {e}"
            )

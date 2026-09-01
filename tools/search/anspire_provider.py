# -*- coding: utf-8 -*-
"""Anspire Search 搜索引擎 Provider

特点：
- 面向 AI 生态的下一代实时智能搜索引擎
- 结果精准、响应快速
- 适用于股票新闻和市场情报搜索

文档：https://open.anspire.cn/document/docs/searchApi/
"""
import logging
from datetime import datetime, timedelta
from typing import List

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class AnspireSearchProvider(BaseSearchProvider):
    """Anspire Search 搜索引擎"""

    name = "Anspire"
    API_ENDPOINT = "https://plugin.anspire.cn/api/ntsearch/search"

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 Anspire 搜索"""
        days = kwargs.get("days", 7)

        headers = {
            'Authorization': f'Bearer {api_key}'
        }

        payload = {
            "query": query,
            "top_k": min(max_results, 50),
            "FromTime": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
            "ToTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        try:
            response = requests.get(
                self.API_ENDPOINT,
                headers=headers,
                params=payload,
                timeout=10
            )

            if response.status_code != 200:
                error_msg = self._parse_error(response)
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            data = response.json()

            if 'code' in data and data.get('code') != 200:
                error_msg = data.get('msg') or f"API 返回错误码: {data.get('code')}"
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            if 'results' not in data:
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message="响应中缺少 results 字段"
                )

            # 解析结果
            results = []
            for item in data.get('results', [])[:max_results]:
                snippet = item.get('content', '')
                if snippet and len(snippet) > 500:
                    snippet = snippet[:500] + "..."

                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=snippet,
                    url=item.get('url', ''),
                    source=self._extract_domain(item.get('url', '')),
                    published_date=item.get('date', '')
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

    def _parse_error(self, response) -> str:
        """解析错误响应"""
        try:
            if response.headers.get('content-type', '').startswith('application/json'):
                error_data = response.json()
                error_message = error_data.get('message', response.text)
            else:
                error_message = response.text

            if response.status_code == 403:
                return f"余额不足或权限不足: {error_message}"
            elif response.status_code == 401:
                return f"API KEY 无效: {error_message}"
            elif response.status_code == 400:
                return f"请求参数错误: {error_message}"
            else:
                return f"HTTP {response.status_code}: {error_message}"
        except Exception:
            return f"HTTP {response.status_code}"

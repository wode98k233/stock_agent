# -*- coding: utf-8 -*-
"""Bocha（博查）搜索引擎 Provider

特点：
- 专为 AI 优化的中文搜索 API
- 结果准确、摘要完整
- 支持时间范围过滤和 AI 摘要
- 兼容 Bing Search API 格式

文档：https://bocha-ai.feishu.cn/wiki/RXEOw02rFiwzGSkd9mUcqoeAnNK
"""
import logging
from typing import List, Optional

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class BochaSearchProvider(BaseSearchProvider):
    """Bocha 搜索引擎"""

    name = "Bocha"
    API_ENDPOINT = "https://api.bocha.cn/v1/web-search"

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 Bocha 搜索"""
        days = kwargs.get("days", 7)

        # 时间范围映射
        freshness_map = {
            1: "oneDay",
            7: "oneWeek",
            30: "oneMonth",
        }
        freshness = "oneWeek"
        for threshold, value in sorted(freshness_map.items()):
            if days <= threshold:
                freshness = value
                break

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        payload = {
            "query": query,
            "freshness": freshness,
            "summary": True,  # 启用 AI 摘要
            "count": min(max_results, 50)
        }

        try:
            response = requests.post(
                self.API_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code != 200:
                error_msg = self._parse_error(response)
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            data = response.json()

            if data.get('code') != 200:
                error_msg = data.get('msg') or f"API 返回错误码: {data.get('code')}"
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            # 解析结果
            results = []
            web_pages = data.get('data', {}).get('webPages', {})
            value_list = web_pages.get('value', [])

            for item in value_list[:max_results]:
                snippet = item.get('summary') or item.get('snippet', '')
                if snippet:
                    snippet = snippet[:500]

                results.append(SearchResult(
                    title=item.get('name', ''),
                    snippet=snippet,
                    url=item.get('url', ''),
                    source=item.get('siteName') or self._extract_domain(item.get('url', '')),
                    published_date=item.get('datePublished'),
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
                return f"余额不足: {error_message}"
            elif response.status_code == 401:
                return f"API KEY 无效: {error_message}"
            elif response.status_code == 400:
                return f"请求参数错误: {error_message}"
            elif response.status_code == 429:
                return f"请求频率限制: {error_message}"
            else:
                return f"HTTP {response.status_code}: {error_message}"
        except Exception:
            return f"HTTP {response.status_code}"

# -*- coding: utf-8 -*-
"""Brave Search 搜索引擎 Provider

特点：
- 隐私优先的独立搜索引擎
- 索引超过 300 亿页面
- 免费层可用
- 支持时间范围过滤

文档：https://brave.com/search/api/
"""
import logging
from typing import List, Optional

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class BraveSearchProvider(BaseSearchProvider):
    """Brave Search 搜索引擎"""

    name = "Brave"
    API_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 Brave 搜索"""
        days = kwargs.get("days", 7)

        # 时间范围映射
        if days <= 1:
            freshness = "pd"  # Past day
        elif days <= 7:
            freshness = "pw"  # Past week
        elif days <= 30:
            freshness = "pm"  # Past month
        else:
            freshness = "py"  # Past year

        headers = {
            'X-Subscription-Token': api_key,
            'Accept': 'application/json'
        }

        params = {
            "q": query,
            "count": min(max_results, 20),
            "freshness": freshness,
            "safesearch": "moderate"
        }

        try:
            response = requests.get(
                self.API_ENDPOINT,
                headers=headers,
                params=params,
                timeout=10
            )

            if response.status_code != 200:
                error_msg = self._parse_error(response)
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            data = response.json()

            # 解析结果
            results = []
            web_data = data.get('web', {})
            web_results = web_data.get('results', [])

            for item in web_results[:max_results]:
                published_date = None
                age = item.get('age') or item.get('page_age')
                if age:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(age.replace('Z', '+00:00'))
                        published_date = dt.strftime('%Y-%m-%d')
                    except (ValueError, AttributeError):
                        published_date = age

                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=item.get('description', '')[:500],
                    url=item.get('url', ''),
                    source=self._extract_domain(item.get('url', '')),
                    published_date=published_date
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
                if 'message' in error_data:
                    return error_data['message']
                if 'error' in error_data:
                    return error_data['error']
                return str(error_data)
            return f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception:
            return f"HTTP {response.status_code}"

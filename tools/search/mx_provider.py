# -*- coding: utf-8 -*-
"""MX 搜索引擎 Provider — 包装现有 mx_search

特点：
- 东方财富妙想搜索
- 无需 API Key（通过 MX_APIKEY 环境变量配置）
- 中文搜索优化
"""
import logging
from typing import List, Optional

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class MXSearchProvider(BaseSearchProvider):
    """东方财富妙想搜索 Provider"""

    name = "MX"

    def __init__(self, api_keys: List[str] = None):
        super().__init__(api_keys)
        self._mx_search = None

    def _get_mx_search(self):
        """延迟加载 MX 搜索"""
        if self._mx_search is None:
            try:
                from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
                self._mx_search = MXSearch()
            except Exception as e:
                logger.warning(f"MX 搜索初始化失败: {e}")
        return self._mx_search

    @property
    def is_available(self) -> bool:
        """MX 搜索通过 MX_APIKEY 环境变量配置"""
        try:
            mx = self._get_mx_search()
            return mx is not None
        except:
            return False

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 MX 搜索"""
        mx = self._get_mx_search()
        if not mx:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="MX 搜索未初始化"
            )

        try:
            result = mx.search(query)
            if not result:
                return SearchResponse(
                    query=query, results=[], provider=self.name, success=True
                )

            # 格式化结果
            formatted = mx.format_pretty(result, max_items=max_results)
            if not formatted:
                return SearchResponse(
                    query=query, results=[], provider=self.name, success=True
                )

            # 转换为统一格式
            results = []
            for item in formatted:
                if isinstance(item, dict):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        snippet=(item.get("content", "") or item.get("summary", ""))[:500],
                        url=item.get("url", ""),
                        source=item.get("source", "mx"),
                        published_date=item.get("date", ""),
                    ))
                elif isinstance(item, str):
                    results.append(SearchResult(
                        title="",
                        snippet=item[:500],
                        url="",
                        source="mx",
                    ))

            return SearchResponse(
                query=query, results=results[:max_results], provider=self.name, success=True
            )

        except Exception as e:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=str(e)
            )

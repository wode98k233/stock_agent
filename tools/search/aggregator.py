# -*- coding: utf-8 -*-
"""搜索引擎聚合器

设计模式：
- 策略模式：遍历多个搜索引擎
- 责任链模式：first-good-wins 策略
- 装饰器模式：缓存 + 并发去重

功能：
1. 内存缓存（10分钟 TTL）
2. 并发去重（相同查询等待首次结果）
3. first-good-wins 策略
"""
import time
import asyncio
import logging
from typing import Optional, List

from .base import BaseSearchProvider, SearchResponse

logger = logging.getLogger("radar.search")


class SearchAggregator:
    """搜索引擎聚合器"""

    def __init__(self, providers: List[BaseSearchProvider] = None):
        self._providers = providers or []
        self._cache: dict[str, tuple[float, SearchResponse]] = {}
        self._cache_ttl = 600  # 10 分钟
        self._inflight: dict[str, asyncio.Event] = {}

    def add_provider(self, provider: BaseSearchProvider):
        """添加搜索引擎"""
        self._providers.append(provider)

    def get_available_providers(self) -> List[BaseSearchProvider]:
        """获取可用的搜索引擎列表"""
        return [p for p in self._providers if p.is_available]

    async def search(self, query: str, max_results: int = 5, **kwargs) -> SearchResponse:
        """执行搜索，first-good-wins 策略

        流程：
        1. 查内存缓存
        2. 并发去重（相同查询等待首次结果）
        3. 遍历 provider：找到直接相关结果则早返回
        """
        # 查缓存
        cache_key = f"{query}:{max_results}"
        cached = self._cache.get(cache_key)
        if cached:
            ts, response = cached
            if time.time() - ts < self._cache_ttl:
                logger.debug(f"[聚合器] 缓存命中: {query}")
                return response

        # 并发去重
        if cache_key in self._inflight:
            await self._inflight[cache_key].wait()
            cached = self._cache.get(cache_key)
            if cached:
                return cached[1]
            return SearchResponse(
                query=query, results=[], provider="aggregator",
                success=False, error_message="等待并发查询失败"
            )

        event = asyncio.Event()
        self._inflight[cache_key] = event

        try:
            response = await self._search_impl(query, max_results, **kwargs)
            self._cache[cache_key] = (time.time(), response)
            return response
        finally:
            event.set()
            self._inflight.pop(cache_key, None)

    async def _search_impl(self, query: str, max_results: int, **kwargs) -> SearchResponse:
        """实际搜索实现"""
        available = self.get_available_providers()
        if not available:
            logger.warning("无可用搜索引擎")
            return SearchResponse(
                query=query, results=[], provider="aggregator",
                success=False, error_message="无可用搜索引擎"
            )

        best_response: Optional[SearchResponse] = None

        for provider in available:
            try:
                response = provider.search(query, max_results=max_results * 2, **kwargs)
                if not response.success or not response.results:
                    continue

                # 如果找到足够多的结果，早返回
                if len(response.results) >= max_results:
                    logger.info(f"[{provider.name}] 返回 {len(response.results)} 条结果")
                    response.results = response.results[:max_results]
                    return response

                # 保留最佳结果
                if not best_response or len(response.results) > len(best_response.results):
                    best_response = response

            except Exception as e:
                logger.warning(f"[{provider.name}] 搜索失败: {e}")
                continue

        if best_response:
            return best_response

        return SearchResponse(
            query=query, results=[], provider="aggregator",
            success=False, error_message="所有搜索引擎均无结果"
        )

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        logger.info("[聚合器] 缓存已清空")

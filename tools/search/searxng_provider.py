# -*- coding: utf-8 -*-
"""SearXNG 搜索引擎 Provider

特点：
- 自建实例，无配额限制
- 支持公共实例自动发现
- 多实例轮转 + 故障转移

文档：https://docs.searxng.org/dev/search_api.html
"""
import time
import logging
import threading
from typing import List, Optional, Tuple, Dict

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class SearXNGSearchProvider(BaseSearchProvider):
    """SearXNG 搜索引擎"""

    name = "SearXNG"
    PUBLIC_INSTANCES_URL = "https://searx.space/data/instances.json"
    PUBLIC_INSTANCES_CACHE_TTL = 3600  # 1 小时
    PUBLIC_INSTANCES_POOL_LIMIT = 20
    PUBLIC_INSTANCES_TIMEOUT = 5
    SELF_HOSTED_TIMEOUT = 10

    # 公共实例缓存（类级）
    _public_instances_cache: Optional[Tuple[float, List[str]]] = None
    _public_instances_lock = threading.Lock()

    def __init__(self, base_urls: Optional[List[str]] = None, use_public_instances: bool = False):
        """
        初始化 SearXNG 搜索引擎

        Args:
            base_urls: 自建实例地址列表
            use_public_instances: 是否使用公共实例
        """
        # SearXNG 不需要 API Key，使用 base_urls 作为 keys
        normalized_urls = [url.rstrip("/") for url in (base_urls or []) if url.strip()]
        super().__init__(normalized_urls)
        self._base_urls = normalized_urls
        self._use_public_instances = bool(use_public_instances and not self._base_urls)
        self._cursor = 0
        self._cursor_lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        return bool(self._base_urls) or self._use_public_instances

    def _get_next_key(self) -> Optional[str]:
        """轮转获取下一个实例 URL"""
        with self._cursor_lock:
            if self._base_urls:
                idx = self._cursor % len(self._base_urls)
                self._cursor = (self._cursor + 1) % len(self._base_urls)
                return self._base_urls[idx]
            return None

    def _record_success(self, key: str):
        """SearXNG 不记录 Key 成功"""
        pass

    def _record_error(self, key: str):
        """SearXNG 不记录 Key 错误"""
        pass

    @classmethod
    def _get_public_instances(cls) -> List[str]:
        """获取公共实例列表（带缓存）"""
        now = time.time()

        with cls._public_instances_lock:
            # 检查缓存
            if cls._public_instances_cache:
                cached_at, cached_urls = cls._public_instances_cache
                if now - cached_at < cls.PUBLIC_INSTANCES_CACHE_TTL:
                    return cached_urls

            # 获取公共实例
            try:
                response = requests.get(cls.PUBLIC_INSTANCES_URL, timeout=cls.PUBLIC_INSTANCES_TIMEOUT)
                if response.status_code == 200:
                    data = response.json()
                    instances = data.get("instances", {})

                    # 筛选可用实例
                    urls = []
                    for url, info in instances.items():
                        if not isinstance(info, dict):
                            continue
                        if info.get("network_type") != "normal":
                            continue
                        http_status = (info.get("http") or {}).get("status_code")
                        if http_status == 200:
                            urls.append(url.rstrip("/"))

                    if urls:
                        cls._public_instances_cache = (now, urls[:cls.PUBLIC_INSTANCES_POOL_LIMIT])
                        logger.info(f"[SearXNG] 已刷新公共实例池，共 {len(urls)} 个")
                        return urls[:cls.PUBLIC_INSTANCES_POOL_LIMIT]

            except Exception as e:
                logger.warning(f"[SearXNG] 获取公共实例失败: {e}")

            # 返回缓存（如果有）
            if cls._public_instances_cache:
                return cls._public_instances_cache[1]
            return []

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 SearXNG 搜索

        api_key 在这里是实例 URL
        """
        days = kwargs.get("days", 7)

        # 时间范围映射
        if days <= 1:
            time_range = "day"
        elif days <= 7:
            time_range = "week"
        elif days <= 30:
            time_range = "month"
        else:
            time_range = "year"

        # 确定搜索实例
        if self._base_urls:
            instances = [api_key] if api_key else self._base_urls
            timeout = self.SELF_HOSTED_TIMEOUT
        elif self._use_public_instances:
            instances = self._get_public_instances()
            timeout = self.PUBLIC_INSTANCES_TIMEOUT
        else:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="未配置 SearXNG 实例"
            )

        if not instances:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="无可用 SearXNG 实例"
            )

        # 尝试每个实例
        errors = []
        for instance in instances[:3]:  # 最多尝试 3 个实例
            try:
                search_url = instance if instance.endswith("/search") else instance + "/search"

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                params = {
                    "q": query,
                    "format": "json",
                    "time_range": time_range,
                    "pageno": 1,
                }

                response = requests.get(search_url, headers=headers, params=params, timeout=timeout)

                if response.status_code != 200:
                    errors.append(f"{instance}: HTTP {response.status_code}")
                    continue

                data = response.json()
                raw_results = data.get("results", [])

                results = []
                for item in raw_results[:max_results]:
                    if not isinstance(item, dict):
                        continue

                    url = item.get("url")
                    if not url:
                        continue

                    published_date = None
                    raw_date = item.get("publishedDate")
                    if raw_date:
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                            published_date = dt.strftime("%Y-%m-%d")
                        except (ValueError, AttributeError):
                            published_date = raw_date

                    results.append(SearchResult(
                        title=item.get("title", ""),
                        snippet=(item.get("content") or item.get("description") or "")[:500],
                        url=url,
                        source=self._extract_domain(url),
                        published_date=published_date,
                    ))

                return SearchResponse(
                    query=query, results=results, provider=self.name, success=True
                )

            except requests.exceptions.Timeout:
                errors.append(f"{instance}: 超时")
            except Exception as e:
                errors.append(f"{instance}: {e}")

        return SearchResponse(
            query=query, results=[], provider=self.name,
            success=False, error_message="；".join(errors[:3])
        )

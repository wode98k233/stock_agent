# -*- coding: utf-8 -*-
"""搜索引擎 Provider 基类

设计模式：
- 策略模式：每个搜索引擎是一个策略
- 模板方法模式：定义搜索流程骨架
- 多 Key 轮转 + 错误计数 + 自动降级
"""
import time
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger("radar.search")


@dataclass
class SearchResult:
    """搜索结果数据类"""
    title: str
    snippet: str
    url: str
    source: str
    published_date: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "source": self.source,
            "published_date": self.published_date,
        }


@dataclass
class SearchResponse:
    """搜索响应"""
    query: str
    results: List[SearchResult]
    provider: str
    success: bool = True
    error_message: Optional[str] = None
    search_time: float = 0.0

    def to_dicts(self) -> List[dict]:
        return [r.to_dict() for r in self.results]


class BaseSearchProvider(ABC):
    """搜索引擎 Provider 基类

    职责：
    1. 管理 API Key 轮转
    2. 记录 Key 错误计数
    3. 定义搜索流程骨架（模板方法）
    """

    name: str = "base"

    def __init__(self, api_keys: List[str] = None):
        self._api_keys = api_keys or []
        self._key_cycle = self._create_key_cycle()
        self._key_errors: Dict[str, int] = {}
        self._key_usage: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _create_key_cycle(self):
        """创建 Key 轮转迭代器"""
        import itertools
        return itertools.cycle(self._api_keys) if self._api_keys else None

    @property
    def is_available(self) -> bool:
        """是否有可用的 Key"""
        return bool(self._api_keys)

    def _get_next_key(self) -> Optional[str]:
        """轮转获取下一个 Key，跳过 error>=3 的 Key"""
        with self._lock:
            if not self._key_cycle:
                return None

            for _ in range(len(self._api_keys)):
                key = next(self._key_cycle)
                if self._key_errors.get(key, 0) < 3:
                    self._key_usage[key] = self._key_usage.get(key, 0) + 1
                    return key

            # 所有 Key 都 exhausted，重置错误计数
            logger.warning(f"[{self.name}] 所有 Key 已达错误上限，重置计数")
            self._key_errors.clear()
            key = next(self._key_cycle)
            self._key_usage[key] = self._key_usage.get(key, 0) + 1
            return key

    def _record_success(self, key: str):
        """记录成功使用"""
        with self._lock:
            if key in self._key_errors and self._key_errors[key] > 0:
                self._key_errors[key] -= 1

    def _record_error(self, key: str):
        """报告 Key 错误"""
        with self._lock:
            self._key_errors[key] = self._key_errors.get(key, 0) + 1
            logger.warning(f"[{self.name}] Key {key[:8]}... 错误计数: {self._key_errors[key]}")

    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取域名"""
        try:
            parsed = urlparse(url)
            return parsed.netloc.replace('www.', '') or '未知来源'
        except Exception:
            return '未知来源'

    @abstractmethod
    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行搜索（子类实现）"""
        ...

    def search(self, query: str, max_results: int = 5, **kwargs) -> SearchResponse:
        """执行搜索（模板方法）

        流程：
        1. 获取 API Key
        2. 调用子类实现
        3. 记录成功/失败
        """
        api_key = self._get_next_key()
        if not api_key:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message=f"{self.name} 未配置 API Key"
            )

        start_time = time.time()
        try:
            response = self._do_search(query, api_key, max_results, **kwargs)
            response.search_time = time.time() - start_time

            if response.success:
                self._record_success(api_key)
                logger.info(f"[{self.name}] 搜索 '{query}' 成功，返回 {len(response.results)} 条，耗时 {response.search_time:.2f}s")
            else:
                self._record_error(api_key)

            return response

        except Exception as e:
            self._record_error(api_key)
            elapsed = time.time() - start_time
            logger.error(f"[{self.name}] 搜索 '{query}' 失败: {e}")
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message=str(e),
                search_time=elapsed,
            )

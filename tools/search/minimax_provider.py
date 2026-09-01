# -*- coding: utf-8 -*-
"""MiniMax Web Search 搜索引擎 Provider

特点：
- 基于 MiniMax Coding Plan 订阅
- 返回结构化的搜索结果
- 支持熔断器保护

API endpoint: POST https://api.minimaxi.com/v1/coding_plan/search
"""
import time
import logging
from typing import List, Optional

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")


class MiniMaxSearchProvider(BaseSearchProvider):
    """MiniMax Web Search 搜索引擎"""

    name = "MiniMax"
    API_ENDPOINT = "https://api.minimaxi.com/v1/coding_plan/search"

    # 熔断器配置
    CB_FAILURE_THRESHOLD = 3
    CB_COOLDOWN_SECONDS = 300  # 5 分钟

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

    @property
    def is_available(self) -> bool:
        """检查可用性（考虑熔断器状态）"""
        if not self._api_keys:
            return False
        if self._consecutive_failures >= self.CB_FAILURE_THRESHOLD:
            if time.time() < self._circuit_open_until:
                return False
        return True

    def _record_success(self, key: str):
        """记录成功（重置熔断器）"""
        super()._record_success(key)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_error(self, key: str):
        """记录错误（触发熔断器）"""
        super()._record_error(key)
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.CB_FAILURE_THRESHOLD:
            self._circuit_open_until = time.time() + self.CB_COOLDOWN_SECONDS
            logger.warning(
                f"[MiniMax] 熔断器开启 - {self._consecutive_failures} 次连续失败，"
                f"冷却 {self.CB_COOLDOWN_SECONDS}s"
            )

    @staticmethod
    def _time_hint(days: int) -> str:
        """构建时间提示字符串"""
        if days <= 1:
            return "今天"
        elif days <= 3:
            return "最近三天"
        elif days <= 7:
            return "最近一周"
        else:
            return "最近一个月"

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        """执行 MiniMax 搜索"""
        days = kwargs.get("days", 7)

        # 增强查询（添加时间提示）
        time_hint = self._time_hint(days)
        augmented_query = f"{query} {time_hint}"

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'MM-API-Source': 'Minimax-MCP',
        }
        payload = {"q": augmented_query}

        try:
            response = requests.post(
                self.API_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=15
            )

            if response.status_code != 200:
                error_msg = self._parse_error(response)
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            data = response.json()

            # 检查 base_resp 状态
            base_resp = data.get('base_resp', {})
            if base_resp.get('status_code', 0) != 0:
                error_msg = base_resp.get('status_msg', 'Unknown API error')
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg
                )

            # 解析结果
            results = []
            for item in data.get('organic', []):
                date_val = item.get('date')

                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=(item.get('snippet', '') or '')[:500],
                    url=item.get('link', ''),
                    source=self._extract_domain(item.get('link', '')),
                    published_date=date_val,
                ))

                if len(results) >= max_results:
                    break

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
            ct = response.headers.get('content-type', '')
            if 'json' in ct:
                err = response.json()
                base_resp = err.get('base_resp', {})
                msg = base_resp.get('status_msg') or err.get('message') or str(err)
                return msg
            return f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception:
            return f"HTTP {response.status_code}"

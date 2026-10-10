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
    # 妙想搜索不通过 api_keys 传凭据（SDK 内部鉴权）→ 必须声明免密，
    # 否则基类 `search()` 会在「未配置 API Key」处静默短路（2026-09-21 实测踩过）
    requires_api_key = False

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
        """执行 MX 搜索（**直接解析 API envelope**）。

        ⚠️ 2026-09-21 修的两处（线上「新闻检索恒为空」的真因）：
        ① 原实现依赖 `mx.format_pretty(result, max_items=...)`，但它返回的是**给人看的
           终端文本（str）**，下面 `for item in formatted` 会**逐字符**遍历 →
           即便走到这一步也只会产出「每条结果只有一个字符」的垃圾；
        ② 解析为空时一律 `success=True, results=[]`，把 provider 侧的问题
           伪装成「查询成功但没有这类新闻」，上层因此只能报「所有搜索引擎均无结果」。

        现在直接读 `data.data.llmSearchResponse.data`，
        字段映射：`title / content / jumpUrl / date / source`（注意是 `jumpUrl` 不是 `url`）。
        额度耗尽与状态码异常都如实回传，便于上层区分「没额度」和「没这类新闻」。
        """
        mx = self._get_mx_search()
        if not mx:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="MX 搜索未初始化"
            )

        try:
            raw = mx.search(query)
        except Exception as e:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=f"MX 搜索调用失败: {e}"
            )

        from tools.other_skills.eastmoney.mx_common import (
            as_dict, is_quota_exhausted, quota_message,
        )

        payload = as_dict(raw)
        # 妙想额度耗尽时顶层 status 仍是 0，只在 data.data.message 带提示 → 必须最先判
        if is_quota_exhausted(raw):
            return SearchResponse(
                query=query, results=[], provider=self.name, success=False,
                error_message=quota_message(raw) or "妙想搜索额度耗尽（不可重试，按日/周恢复）",
            )
        status = payload.get("status")
        if status not in (0, None):
            return SearchResponse(
                query=query, results=[], provider=self.name, success=False,
                error_message=f"MX 状态码 {status}: {payload.get('message', '')}",
            )

        inner = as_dict(as_dict(payload.get("data")).get("data"))
        items = as_dict(inner.get("llmSearchResponse")).get("data") or []

        results = []
        for raw_item in items[:max_results]:
            item = as_dict(raw_item)
            results.append(SearchResult(
                title=str(item.get("title") or "").strip(),
                snippet=str(item.get("content") or "")[:500],
                url=str(item.get("jumpUrl") or item.get("url") or ""),
                source=str(item.get("source") or "mx"),
                published_date=str(item.get("date") or ""),
            ))

        if not results:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="未找到相关资讯"
            )
        return SearchResponse(query=query, results=results, provider=self.name, success=True)

# -*- coding: utf-8 -*-
"""搜索引擎工厂

设计模式：工厂模式

职责：
1. 根据环境变量创建搜索引擎实例
2. 自动发现可用的搜索引擎
3. 返回配置好的 SearchAggregator
"""
import os
import logging
from typing import List, Optional

from .base import BaseSearchProvider
from .aggregator import SearchAggregator
from .mx_provider import MXSearchProvider
from .tavl_provider import TavilySearchProvider
from .serp_provider import SerpAPISearchProvider
from .bocha_provider import BochaSearchProvider
from .brave_provider import BraveSearchProvider
from .searxng_provider import SearXNGSearchProvider
from .anspire_provider import AnspireSearchProvider
from .minimax_provider import MiniMaxSearchProvider
from .parallel_provider import ParallelSearchProvider
from .exa_provider import ExaSearchProvider

logger = logging.getLogger("radar.search")


class SearchProviderFactory:
    """搜索引擎工厂"""

    @staticmethod
    def _parse_keys(env_var: str) -> List[str]:
        """从环境变量解析 API Key 列表"""
        keys_str = os.getenv(env_var, "")
        if not keys_str:
            return []
        return [k.strip() for k in keys_str.split(",") if k.strip()]

    @staticmethod
    def create_providers() -> List[BaseSearchProvider]:
        """创建所有可用的搜索引擎"""
        providers = []

        # 1. MX 搜索（默认，无需 API Key）
        mx = MXSearchProvider()
        if mx.is_available:
            providers.append(mx)
            logger.info("已配置 MX 搜索")

        # 2. Tavily
        tavily_keys = SearchProviderFactory._parse_keys("TAVILY_API_KEYS")
        if tavily_keys:
            providers.append(TavilySearchProvider(tavily_keys))
            logger.info(f"已配置 Tavily 搜索，共 {len(tavily_keys)} 个 Key")

        # 3. SerpAPI
        serpapi_keys = SearchProviderFactory._parse_keys("SERPAPI_API_KEYS")
        if serpapi_keys:
            providers.append(SerpAPISearchProvider(serpapi_keys))
            logger.info(f"已配置 SerpAPI 搜索，共 {len(serpapi_keys)} 个 Key")

        # 4. Bocha（博查）
        bocha_keys = SearchProviderFactory._parse_keys("BOCHA_API_KEYS")
        if bocha_keys:
            providers.append(BochaSearchProvider(bocha_keys))
            logger.info(f"已配置 Bocha 搜索，共 {len(bocha_keys)} 个 Key")

        # 5. Brave Search
        brave_keys = SearchProviderFactory._parse_keys("BRAVE_API_KEYS")
        if brave_keys:
            providers.append(BraveSearchProvider(brave_keys))
            logger.info(f"已配置 Brave 搜索，共 {len(brave_keys)} 个 Key")

        # 6. Anspire Search
        anspire_keys = SearchProviderFactory._parse_keys("ANSPIRE_API_KEYS")
        if anspire_keys:
            providers.append(AnspireSearchProvider(anspire_keys))
            logger.info(f"已配置 Anspire 搜索，共 {len(anspire_keys)} 个 Key")

        # 7. MiniMax Search
        minimax_keys = SearchProviderFactory._parse_keys("MINIMAX_API_KEYS")
        if minimax_keys:
            providers.append(MiniMaxSearchProvider(minimax_keys))
            logger.info(f"已配置 MiniMax 搜索，共 {len(minimax_keys)} 个 Key")

        # 8/9. Parallel / Exa —— 免 Key 托管 MCP（境外），作为**兜底源**
        # 排在所有需要 Key 的付费源之后：付费源是用户主动配置的、额度与质量更可控；
        # 免 Key 属于厂商免费档（有频率限制、非 SLA），适合在 MX 额度耗尽或付费源未配时顶上。
        # 注意：DDG 系免 Key MCP 在国内不可达（2026-10-06 实测），只有这两个托管版可用。
        # 用 `SEARCH_FREE_MCP_ENABLED=false` 可整体关闭（数据出境合规开关）。
        parallel = ParallelSearchProvider()
        if parallel.is_available:
            providers.append(parallel)
            logger.info("已配置 Parallel 搜索（免 Key，MCP 托管）")

        exa = ExaSearchProvider()
        if exa.is_available:
            providers.append(exa)
            logger.info("已配置 Exa 搜索（免 Key，MCP 托管）")

        # 10. SearXNG（自建实例）
        searxng_urls = os.getenv("SEARXNG_URLS", "")
        searxng_url_list = [u.strip() for u in searxng_urls.split(",") if u.strip()] if searxng_urls else []
        searxng_public = os.getenv("SEARXNG_PUBLIC_INSTANCES", "false").lower() == "true"

        searxng = SearXNGSearchProvider(
            base_urls=searxng_url_list,
            use_public_instances=searxng_public
        )
        if searxng.is_available:
            providers.append(searxng)
            if searxng_url_list:
                logger.info(f"已配置 SearXNG 搜索，共 {len(searxng_url_list)} 个实例")
            else:
                logger.info("已启用 SearXNG 公共实例模式")

        if not providers:
            logger.warning("未配置任何搜索引擎，搜索功能将不可用")

        return providers

    @staticmethod
    def create_aggregator() -> SearchAggregator:
        """创建配置好的搜索引擎聚合器"""
        providers = SearchProviderFactory.create_providers()
        return SearchAggregator(providers)

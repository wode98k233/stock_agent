# -*- coding: utf-8 -*-
"""搜索引擎聚合模块

支持的搜索引擎：
- MX（妙想）- 默认，无需 API Key
- Tavily - 专为 AI 优化
- SerpAPI - 支持 Google/Bing/百度
- Bocha（博查）- 中文搜索优化
- Brave - 隐私优先
- SearXNG - 自建实例，无配额限制
- Anspire - 实时智能搜索
- MiniMax - Coding Plan Web Search

使用方式：
    from tools.search import SearchProviderFactory

    # 创建聚合器
    aggregator = SearchProviderFactory.create_aggregator()

    # 执行搜索
    response = await aggregator.search("贵州茅台 股价", max_results=5)
"""
from .base import BaseSearchProvider, SearchResult, SearchResponse
from .aggregator import SearchAggregator
from .factory import SearchProviderFactory
from .mx_provider import MXSearchProvider
from .serp_provider import SerpAPISearchProvider
from .tavl_provider import TavilySearchProvider
from .bocha_provider import BochaSearchProvider
from .brave_provider import BraveSearchProvider
from .searxng_provider import SearXNGSearchProvider
from .anspire_provider import AnspireSearchProvider
from .minimax_provider import MiniMaxSearchProvider

__all__ = [
    # 基类
    "BaseSearchProvider",
    "SearchResult",
    "SearchResponse",
    # 聚合器
    "SearchAggregator",
    # 工厂
    "SearchProviderFactory",
    # 搜索引擎
    "MXSearchProvider",
    "SerpAPISearchProvider",
    "TavilySearchProvider",
    "BochaSearchProvider",
    "BraveSearchProvider",
    "SearXNGSearchProvider",
    "AnspireSearchProvider",
    "MiniMaxSearchProvider",
]

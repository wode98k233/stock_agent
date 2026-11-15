"""数据源与搜索源（凭证 / 优先级 / 超时）（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _DatasourceMixin:
    MX_APIKEY = os.getenv("MX_APIKEY", "")

    IWENCAI_API_KEY = os.getenv("IWENCAI_API_KEY", "")

    IWENCAI_BASE_URL = os.getenv("IWENCAI_BASE_URL", "https://openapi.iwencai.com")

    TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")

    HITHINK_FINANCE_API_KEY = os.getenv("HITHINK_FINANCE_API_KEY", "")

    HITHINK_PRIORITY = int(os.getenv("HITHINK_PRIORITY", "95"))

    # 搜索引擎配置（逗号分隔多个 Key）

    SERPAPI_API_KEYS = os.getenv("SERPAPI_API_KEYS", "")

    TAVILY_API_KEYS = os.getenv("TAVILY_API_KEYS", "")

    SEARCH_FREE_MCP_ENABLED = os.getenv("SEARCH_FREE_MCP_ENABLED", "true").lower() == "true"

    # DeepSeek 官方 Search / Fetch（api.deepseek.com/v1/tools/*），按请求次数计费（¥5/1000 次）。
    # Key 单独配：LLM 走中继时（如 opencode zen）那把 Key 在官方站点无效，不能复用。
    DEEPSEEK_API_KEYS = os.getenv("DEEPSEEK_API_KEYS", "")

    DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")

    DEEPSEEK_SEARCH_ENABLED = os.getenv("DEEPSEEK_SEARCH_ENABLED", "true").lower() == "true"

    DEEPSEEK_SEARCH_NEED_CONTENT = os.getenv("DEEPSEEK_SEARCH_NEED_CONTENT", "false").lower() == "true"

    DEEPSEEK_FETCH_MAX_CHARS = int(os.getenv("DEEPSEEK_FETCH_MAX_CHARS", "8000"))

    # 搜索源顺序 / 白名单（逗号分隔引擎名，如 "DeepSeek,MX"；留空 = 默认顺序全量使用）
    SEARCH_PROVIDER_ORDER = os.getenv("SEARCH_PROVIDER_ORDER", "")

    # Longbridge 配置

    LONGBRIDGE_APP_KEY = os.getenv("LONGBRIDGE_APP_KEY", "")

    LONGBRIDGE_APP_SECRET = os.getenv("LONGBRIDGE_APP_SECRET", "")

    LONGBRIDGE_ACCESS_TOKEN = os.getenv("LONGBRIDGE_ACCESS_TOKEN", "")

    # Finnhub 配置（美股数据源）

    FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

    # LLM 高级配置

    AKSHARE_ENABLE_EASTMONEY_PATCH = os.getenv("AKSHARE_ENABLE_EASTMONEY_PATCH", "true").lower() == "true"

    # 数据库路径 - 动态获取，支持打包部署

    DATASOURCE_MAX_FAILS = int(os.getenv("DATASOURCE_MAX_FAILS", "5"))
    # 逗号分隔的数据源名顺序；留空表示用各源内置 priority

    DATASOURCE_PRIORITY_ORDER = os.getenv("DATASOURCE_PRIORITY_ORDER", "")

    # Web 服务


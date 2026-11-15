"""主 LLM / 网关 / 语义缓存 / 模型路由（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _LLMMixin:

    # LLM
    # 单次 LLM 请求超时（秒）。SDK 默认 read=600s 且自带 2 次重试 —— 最坏
    # 30 分钟静默无响应（实测撞到过：日志停在「LLM 调用 #N」十几分钟无任何输出，
    # 连 429 重试日志都没有，就是连接层挂死）。收紧到 300s 后最坏 ~15 分钟。
    LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "300"))

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

    OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")

    OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "gpt-4o")

    OPENAI_HEADERS = os.getenv("OPENAI_HEADERS", "")

    OPENAI_DEFAULT_PARAMS = os.getenv("OPENAI_DEFAULT_PARAMS", "")

    OPENAI_REASONING_CONTENT_POLICY = os.getenv("OPENAI_REASONING_CONTENT_POLICY", "auto").lower()

    OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "medium").strip().lower()

    EXTRA_BODY = os.getenv("EXTRA_BODY", "")

    CACHE_PREFIX_ENABLED = os.getenv("CACHE_PREFIX_ENABLED", "false").lower() == "true"

    CACHE_PREFIX_CONTENT = os.getenv("CACHE_PREFIX_CONTENT", "")

    # A8 语义缓存 + 模型路由（默认关闭灰度，配置面板可开）

    LLM_CACHE_ENABLED = os.getenv("LLM_CACHE_ENABLED", "false").lower() == "true"

    LLM_CACHE_TTL_MAP = os.getenv("LLM_CACHE_TTL_MAP", '{"market":1800,"sector_intraday":0,"sector_offhours":14400,"methodology":604800,"data_query":0}')

    LLM_CACHE_CATEGORY_MAP = os.getenv("LLM_CACHE_CATEGORY_MAP", '{"market_quick":"market","market_overview":"market","sector_analysis":"sector","stock_analysis":"sector","methodology":"methodology","data_query":"data_query","default":"sector"}')

    LLM_ROUTING_ENABLED = os.getenv("LLM_ROUTING_ENABLED", "false").lower() == "true"

    LLM_ROUTING_MAP = os.getenv("LLM_ROUTING_MAP", '{"rules":[{"match":{"scenario":["market_quick","data_query"]},"tier":"fast"},{"match":{"label":["report_enhance","deep_research","debate"]},"tier":"strong"},{"match":{"purpose":["compress","judge"]},"tier":"fast"}],"default":"standard"}')

    LLM_TIER_FAST_MODEL = os.getenv("LLM_TIER_FAST_MODEL", "")

    LLM_TIER_STANDARD_MODEL = os.getenv("LLM_TIER_STANDARD_MODEL", "")

    LLM_TIER_STRONG_MODEL = os.getenv("LLM_TIER_STRONG_MODEL", "")

    # Agent 通用配置


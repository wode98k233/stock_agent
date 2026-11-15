"""Web 服务 / 全局总览大屏 / 成本计价 / Trace（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _WebMixin:
    ENABLE_TRACE = os.getenv("ENABLE_TRACE", "false").lower() == "true"

    TRACE_DB_PATH = os.getenv("TRACE_DB_PATH", "")

    # 开发模式：每次请求前热重载 Agent 模块（代码修改即时生效，无需重启）

    WEB_DEV_RELOAD = os.getenv("WEB_DEV_RELOAD", "false").lower() == "true"

    # 界面：侧边栏显示溯源工具（回答溯源/日志关联面板），默认隐藏（面向开发者的调试功能）

    WEB_SHOW_TRACE_TOOLS = os.getenv("WEB_SHOW_TRACE_TOOLS", "false").lower() == "true"

    # 界面：首轮对话由 classify 用 LLM 生成会话标题并回填（仅生成一次，成功后不再覆盖）

    WEB_DIALOG_AUTO_TITLE = os.getenv("WEB_DIALOG_AUTO_TITLE", "true").lower() == "true"

    # 分析框架引擎

    WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")

    WEB_PORT = os.getenv("WEB_PORT", "8000")

    WEB_MAX_WORKERS = int(os.getenv("WEB_MAX_WORKERS", "8"))

    # 系统监控

    MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "2"))

    # 盯盘告警（默认关闭：升级后行为与现状完全一致）

    OVERVIEW_ENABLED = os.getenv("OVERVIEW_ENABLED", "true").lower() == "true"

    OVERVIEW_BLOCK_CACHE_TTL_S = int(os.getenv("OVERVIEW_BLOCK_CACHE_TTL_S", "60"))

    OVERVIEW_MARKET_REFRESH_S = int(os.getenv("OVERVIEW_MARKET_REFRESH_S", "60"))

    OVERVIEW_RUNTIME_REFRESH_S = int(os.getenv("OVERVIEW_RUNTIME_REFRESH_S", "10"))

    OVERVIEW_SCAN_TTL_S = int(os.getenv("OVERVIEW_SCAN_TTL_S", "600"))

    OVERVIEW_SCAN_DAYS = int(os.getenv("OVERVIEW_SCAN_DAYS", "120"))

    OVERVIEW_SCAN_STRATEGIES = os.getenv("OVERVIEW_SCAN_STRATEGIES", "")

    OVERVIEW_SCAN_REFRESH_BUDGET_S = float(os.getenv("OVERVIEW_SCAN_REFRESH_BUDGET_S", "20"))

    OVERVIEW_SCAN_REFRESH_MAX = int(os.getenv("OVERVIEW_SCAN_REFRESH_MAX", "6"))

    OVERVIEW_MACRO_INDICATORS = os.getenv("OVERVIEW_MACRO_INDICATORS", "")

    OVERVIEW_WATCHLIST_BUDGET_S = float(os.getenv("OVERVIEW_WATCHLIST_BUDGET_S", "8"))

    OVERVIEW_TOKEN_DAYS = int(os.getenv("OVERVIEW_TOKEN_DAYS", "14"))

    OVERVIEW_TOKEN_MODEL = os.getenv("OVERVIEW_TOKEN_MODEL", "DeepSeek v4.1 Flash")

    OVERVIEW_TOKEN_PRICE_IN = float(os.getenv("OVERVIEW_TOKEN_PRICE_IN", "2.0"))

    OVERVIEW_TOKEN_PRICE_OUT = float(os.getenv("OVERVIEW_TOKEN_PRICE_OUT", "8.0"))

    OVERVIEW_TOKEN_PRICE_CACHED = float(os.getenv("OVERVIEW_TOKEN_PRICE_CACHED", "0.2"))

    # 成本 / 计价：价目（供应商 → 模型 → 峰谷价）在 data/pricing/*.json（页面可编辑），
    # 这里只放标量开关。OVERVIEW_TOKEN_PRICE_* 保留为价目文件缺失时的兜底。

    COST_TRACKING_ENABLED = os.getenv("COST_TRACKING_ENABLED", "true").lower() == "true"

    COST_PRICING_CATALOG_PATH = os.getenv("COST_PRICING_CATALOG_PATH", "data/pricing/pricing_catalog.json")

    COST_CURRENCY = os.getenv("COST_CURRENCY", "CNY")

    COST_PEAK_TZ = os.getenv("COST_PEAK_TZ", "Asia/Shanghai")

    COST_UNKNOWN_PROVIDER_POLICY = os.getenv("COST_UNKNOWN_PROVIDER_POLICY", "warn")

    COST_FREE_VALIDITY_CHECK = os.getenv("COST_FREE_VALIDITY_CHECK", "true").lower() == "true"

    # 后台巡检：启动后异步跑（首次延迟错开路由导入与各 warmup，避免 GIL 争抢）


    # 对话图片上传（v1：图片转文字进对话）
    UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "5"))
    UPLOAD_RETENTION_DAYS = int(os.getenv("UPLOAD_RETENTION_DAYS", "45"))
    UPLOAD_MAX_TOTAL_MB = int(os.getenv("UPLOAD_MAX_TOTAL_MB", "500"))
    # 图片转述最大字数（提示词里的输出长度约束）
    UPLOAD_CAPTION_MAX_CHARS = int(os.getenv("UPLOAD_CAPTION_MAX_CHARS", "500"))

"""配置面板 Schema 定义和辅助函数。"""
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = {
    "BOCHA_API_KEYS", "BRAVE_API_KEYS", "ANSPIRE_API_KEYS", "MINIMAX_API_KEYS",
    "DINGTALK_WEBHOOK_SECRET", "FEISHU_WEBHOOK_SECRET",
    "TELEGRAM_BOT_TOKEN", "EMAIL_SMTP_PASSWORD",
    "PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN",
    "GOTIFY_TOKEN", "PUSHPLUS_TOKEN", "SERVERCHAN3_SENDKEY",
    "HITHINK_FINANCE_API_KEY",
    # Embedding / 评测等密钥：接口不返回明文、页面不可改、apply 接口拒绝写入
    "STOCK_MEMORY_API_KEY", "STOCK_MEMORY_RERANKER_API_KEY", "JUDGE_LLM_API_KEY",
}


def mask_sensitive(key: str, value: Any) -> str:
    if key not in _SENSITIVE_KEYS:
        return str(value)
    s = str(value)
    if not s:
        return ""
    if len(s) <= 6:
        return "***"
    return s[:3] + "-***"


def get_env_path() -> str:
    from utils.app_paths import get_app_dir
    return str(Path(get_app_dir()) / ".env")


def _agent_factory_available() -> list[str]:
    try:
        from agents import AgentFactory, register_all
        model_list = AgentFactory.list()
        if not model_list:
            register_all()
            model_list = AgentFactory.list()
        return model_list
    except Exception:
        return ["react_stock"]


def get_current_mode() -> str:
    try:
        from agents import AgentFactory
        names = AgentFactory.list()
        current = getattr(AgentFactory, "_current", None)
        if current and current in names:
            return current
        return "react_stock" if "react_stock" in names else (names[0] if names else "react_stock")
    except Exception:
        return "react_stock"


def _build_config_schema() -> list[dict]:
    """构建配置 schema（延迟加载 AgentFactory）。"""
    from agents import AgentFactory
    return [
        {"key": "OPENAI_API_BASE", "label": "API Base URL", "type": "str", "group": "LLM"},
        {"key": "OPENAI_MODEL_NAME", "label": "模型名称", "type": "str", "group": "LLM"},
        {"key": "OPENAI_REASONING_CONTENT_POLICY", "label": "推理内容策略", "type": "str", "group": "LLM", "choices": ["auto", "always", "never"]},
        {"key": "OPENAI_REASONING_EFFORT", "label": "思考强度", "type": "str", "group": "LLM", "choices": ["low", "medium", "high", "none", ""], "description": "推理模型思考强度（DeepSeek V4 Flash / GLM-5.2 等），none=关闭思考，留空=不发送参数（老模型兼容）"},
        {"key": "OPENAI_HEADERS", "label": "自定义请求头 (JSON)", "type": "str", "group": "LLM"},
        {"key": "OPENAI_DEFAULT_PARAMS", "label": "默认参数 (JSON)", "type": "str", "group": "LLM"},
        {"key": "EXTRA_BODY", "label": "额外请求体 (JSON)", "type": "str", "group": "LLM"},
        {"key": "CACHE_PREFIX_ENABLED", "label": "启用 LLM 缓存前缀", "type": "bool", "group": "LLM"},
        {"key": "CACHE_PREFIX_CONTENT", "label": "自定义缓存前缀", "type": "str", "group": "LLM"},
        {"key": "AGENT_MODE", "label": "Agent 模式", "type": "str", "group": "Agent 通用", "choices": list(AgentFactory.list()) if _agent_factory_available() else ["react_stock"]},
        {"key": "BATCH_SIZE", "label": "批处理大小", "type": "int", "group": "Agent 通用"},
        {"key": "MEMORY_ENABLED", "label": "启用对话记忆", "type": "bool", "group": "Agent 通用", "description": "控制 ConversationSummaryBufferMemory 开关"},
        {"key": "MEMORY_MAX_TOKENS", "label": "记忆压缩 Token 上限", "type": "int", "group": "Agent 通用"},
        {"key": "INFO_ACCUMULATOR_COMPRESS_THRESHOLD", "label": "信息累积压缩阈值", "type": "int", "group": "Agent 通用"},
        {"key": "REACT_TOOL_CALLS", "label": "工具调用上限", "type": "int", "group": "ReAct"},
        {"key": "REACT_ENABLE_CONTEXT_COMPACTION", "label": "上下文压缩", "type": "bool", "group": "上下文压缩"},
        {"key": "REACT_CONTEXT_RECENT_ROUNDS", "label": "保留完整轮次", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_SUMMARY_TRIGGER_ROUNDS", "label": "摘要触发轮次", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_SUMMARY_PENDING_CHARS", "label": "摘要触发字符", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_SUMMARY_MAX_CHARS", "label": "摘要最大字符", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_DEDUP_MODE", "label": "去重模式", "type": "str", "group": "上下文压缩", "choices": ["exact", "off"]},
        {"key": "REACT_CONTEXT_DEBUG_LOG", "label": "压缩调试日志", "type": "bool", "group": "上下文压缩"},
        {"key": "PLAN_MAX_STEPS", "label": "最大规划步骤数", "type": "int", "group": "Plan / PDOR / Unified"},
        {"key": "PLAN_EXECUTOR_MAX_RETRIES", "label": "Executor 重试次数", "type": "int", "group": "Plan / PDOR / Unified"},
        {"key": "PLAN_EXECUTOR_TOOL_CALLS", "label": "子 ReAct 工具调用上限", "type": "int", "group": "Plan / PDOR / Unified"},
        {"key": "GROUP_MAX_CONCURRENT_AGENTS", "label": "最大并发 Agent 数", "type": "int", "group": "Agent 群模式"},
        {"key": "GROUP_AGENT_MAX_RETRIES", "label": "Agent 最大重试次数", "type": "int", "group": "Agent 群模式"},
        {"key": "GROUP_MAX_REPLAN_COUNT", "label": "最大重规划次数", "type": "int", "group": "Agent 群模式"},
        {"key": "GROUP_MAX_STEPS", "label": "最大执行步骤数", "type": "int", "group": "Agent 群模式"},
        {"key": "MAX_TOKENS_PER_QUERY", "label": "单次查询 Token 上限", "type": "int", "group": "预算"},
        {"key": "MAX_LLM_CALLS_PER_QUERY", "label": "单次查询 LLM 调用上限", "type": "int", "group": "预算"},
        {"key": "MAX_TIME_SECONDS", "label": "最大执行时间(秒)", "type": "int", "group": "预算"},
        {"key": "BUDGET_EXEMPT_CALLS_LIMIT", "label": "豁免窗口调用次数", "type": "int", "group": "预算"},
        {"key": "BUDGET_EXEMPT_TIME_LIMIT", "label": "豁免窗口时间(秒)", "type": "int", "group": "预算"},
        {"key": "CHECKPOINT_BACKEND", "label": "快照后端", "type": "str", "group": "预算", "choices": ["memory", "sqlite", "pg"]},
        {"key": "CACHE_EXPIRE_HOURS", "label": "缓存过期时间(小时)", "type": "int", "group": "调试"},
        {"key": "DATASOURCE_MAX_FAILS", "label": "数据源最大失败次数", "type": "int", "group": "数据源"},
        {"key": "AKSHARE_RATE_LIMIT_MIN", "label": "AkShare 最小请求间隔(秒)", "type": "int", "group": "数据源"},
        {"key": "AKSHARE_RATE_LIMIT_MAX", "label": "AkShare 最大请求间隔(秒)", "type": "int", "group": "数据源"},
        {"key": "AKSHARE_REALTIME_CACHE_TTL", "label": "实时行情缓存时间(秒)", "type": "int", "group": "数据源"},
        {"key": "AKSHARE_ENABLE_EASTMONEY_PATCH", "label": "启用东财防封补丁", "type": "bool", "group": "数据源"},
        {"key": "PYTDX_PRIORITY", "label": "Pytdx 优先级", "type": "int", "group": "数据源"},
        {"key": "PYTDX_CONNECTION_COOLDOWN", "label": "Pytdx 连接冷却(秒)", "type": "int", "group": "数据源"},
        {"key": "PYTDX_SERVERS", "label": "Pytdx 服务器列表", "type": "str", "group": "数据源"},
        {"key": "HITHINK_FINANCE_API_KEY", "label": "同花顺 API Key", "type": "str", "group": "数据源", "description": "同花顺官方金融数据服务 Key（fuyao.aicubes.cn/admin/ 签发），留空禁用该源"},
        {"key": "HITHINK_PRIORITY", "label": "同花顺优先级", "type": "int", "group": "数据源"},
        {"key": "REPORT_ENABLE_ANALYSIS_ENGINE", "label": "启用分析引擎", "type": "bool", "group": "报告"},
        {"key": "REPORT_TEMPLATE", "label": "报告模板", "type": "str", "group": "报告", "choices": ["lightweight", "standard", "professional"]},
        {"key": "REPORT_TEMPLATE_CONTINUATION_MODE", "label": "模板续问判定模式", "type": "str", "group": "报告", "choices": ["keyword", "llm"], "description": "keyword=基于历史的规则续问(零成本,默认)；llm=调用 LLM 判定续问并选模板(更准,有单次调用成本)"},
        {"key": "REPORT_MODE", "label": "报告合成模式", "type": "str", "group": "报告", "choices": ["fast", "full"]},
        {"key": "REPORT_TIME_HORIZON", "label": "时间框架", "type": "str", "group": "报告", "choices": ["short", "mid", "long"]},
        {"key": "REPORT_MIN_TOKENS", "label": "报告最低 Token", "type": "int", "group": "报告"},
        {"key": "REPORT_MAX_TOOL_OUTPUT_CHARS", "label": "工具输出截断(字符)", "type": "int", "group": "报告"},
        {"key": "REPORT_MAX_NEWS_CHARS", "label": "新闻截断(字符)", "type": "int", "group": "报告"},
        {"key": "REPORT_LLM_MODEL", "label": "报告专用模型", "type": "str", "group": "报告"},
        {"key": "REPORT_LLM_API_BASE", "label": "报告专用 API Base", "type": "str", "group": "报告"},
        {"key": "COMPRESS_LLM_MODEL", "label": "压缩专用模型", "type": "str", "group": "报告"},
        {"key": "COMPRESS_LLM_API_BASE", "label": "压缩专用 API Base", "type": "str", "group": "报告"},
        {"key": "JUDGE_LLM_MODEL", "label": "评测 Judge 模型", "type": "str", "group": "报告"},
        {"key": "JUDGE_LLM_API_KEY", "label": "Judge 专用 API Key", "type": "str", "group": "报告", "sensitive": True},
        {"key": "JUDGE_LLM_API_BASE", "label": "Judge 专用 API Base", "type": "str", "group": "报告"},
        {"key": "TOOL_OUTPUT_COMPRESS_THRESHOLD", "label": "工具输出压缩阈值(字符)", "type": "int", "group": "报告"},
        {"key": "TOOL_COMPRESS_THRESHOLDS", "label": "Per-tool 压缩阈值 (JSON)", "type": "str", "group": "报告"},
        {"key": "TOOL_HARD_TRUNCATE_CHARS", "label": "工具输出硬截断上限(字符)", "type": "int", "group": "报告"},
        {"key": "LOG_LEVEL", "label": "日志级别", "type": "str", "group": "调试", "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]},
        {"key": "DEBUG_STEP_CONFIRM", "label": "调试步骤确认", "type": "bool", "group": "调试"},
        {"key": "ENABLE_TRACE", "label": "启用追踪", "type": "bool", "group": "调试"},
        {"key": "WEB_DEV_RELOAD", "label": "开发热重载", "type": "bool", "group": "调试"},
        {"key": "WEB_SHOW_TRACE_TOOLS", "label": "显示溯源工具按钮", "type": "bool", "group": "调试", "description": "侧边栏「日志关联」入口（回答溯源·执行归因面板），默认隐藏；面向开发者调试用"},
        # ── 长期记忆 — 基础（始终生效，管控成本低） ──
        {"key": "JIEBA_ENABLED", "label": "jieba 中文分词", "type": "bool", "group": "长期记忆-基础", "description": "禁用节省 ~40MB，中文分词退化为逐字匹配"},
        {"key": "STOCK_MEMORY_BACKEND", "label": "检索后端", "type": "str", "group": "长期记忆-基础", "choices": ["auto", "fts5", "embedding", "hybrid"], "description": "auto=有 Embedding 则混合，否则纯文本。fts5 模式完全不需要 Embedding"},
        {"key": "MEMORY_RETRIEVAL_TOP_K", "label": "检索返回条数", "type": "int", "group": "长期记忆-基础"},
        {"key": "MEMORY_MAX_PROMPT_TOKENS", "label": "注入 Prompt 最大字符", "type": "int", "group": "长期记忆-基础"},
        {"key": "MEMORY_CONFIDENCE_THRESHOLD", "label": "置信度门槛 (0-1)", "type": "float", "group": "长期记忆-基础", "description": "低于此值的记忆不注入 Prompt；0=关闭过滤"},
        {"key": "MEMORY_EPISODIC_RETENTION_DAYS", "label": "情景记忆保留天数", "type": "int", "group": "长期记忆-基础"},
        {"key": "MEMORY_AUTO_CLEANUP_INTERVAL_MINUTES", "label": "自动清理间隔(分)", "type": "int", "group": "长期记忆-基础", "description": "0=禁用自动清理"},

        # ── 长期记忆 — Embedding（可选，≈100-500MB。留空模式即关闭） ──
        {"key": "STOCK_MEMORY_EMBEDDING", "label": "▶ Embedding 模式", "type": "str", "group": "长期记忆-Embedding", "choices": ["", "openai", "remote", "local", "ollama"], "description": "留空=关闭矢量检索，仅用FTS5文本搜索。openai/remote=远程API，ollama=本地Ollama，local=进程内加载模型(~500MB)"},
        {"key": "STOCK_MEMORY_API_KEY", "label": "API Key (与Reranker共用)", "type": "str", "group": "长期记忆-Embedding", "sensitive": True, "description": "远程模式必填；留空回退 OPENAI_API_KEY"},
        {"key": "STOCK_MEMORY_API_BASE", "label": "API Base URL (与Reranker共用)", "type": "str", "group": "长期记忆-Embedding", "description": "远程模式必填；留空回退 OPENAI_API_BASE。注意勿指向Reranker专用服务"},
        {"key": "STOCK_MEMORY_EMBEDDING_MODEL", "label": "远程模型名", "type": "str", "group": "长期记忆-Embedding", "description": "openai/remote 模式使用"},
        {"key": "STOCK_MEMORY_LOCAL_MODEL", "label": "本地模型名", "type": "str", "group": "长期记忆-Embedding", "description": "local 模式使用，如 BAAI/bge-small-zh-v1.5"},
        {"key": "STOCK_MEMORY_EMBEDDING_READY_TIMEOUT", "label": "就绪等待超时(秒)", "type": "float", "group": "长期记忆-Embedding", "description": "等待 Embedding 服务就绪的最长时间，超时自动降级"},
        {"key": "STOCK_MEMORY_EMBEDDING_READY_INTERVAL", "label": "就绪探针间隔(秒)", "type": "float", "group": "长期记忆-Embedding"},

        # ── 长期记忆 — Reranker（可选增强，≈2GB本地模型。留空模式即关闭） ──
        {"key": "STOCK_MEMORY_RERANKER", "label": "▶ Reranker 模式", "type": "str", "group": "长期记忆-Reranker", "choices": ["", "local", "openai"], "description": "留空=关闭精排。local=进程内加载CrossEncoder(~2GB)或本地路径，openai=远程 /v1/rerank API"},
        {"key": "STOCK_MEMORY_RERANKER_API_BASE", "label": "Reranker API Base", "type": "str", "group": "长期记忆-Reranker", "description": "openai 模式必填。独立于 Embedding，不会共用 STOCK_MEMORY_API_BASE"},
        {"key": "STOCK_MEMORY_RERANKER_API_KEY", "label": "Reranker API Key", "type": "str", "group": "长期记忆-Reranker", "sensitive": True, "description": "留空回退 STOCK_MEMORY_API_KEY → OPENAI_API_KEY"},
        {"key": "STOCK_MEMORY_RERANKER_MODEL", "label": "模型名", "type": "str", "group": "长期记忆-Reranker", "description": "local: HuggingFace模型名; openai: 传给API的model参数"},
        {"key": "STOCK_MEMORY_RERANKER_LOCAL_DIR", "label": "本地模型路径", "type": "str", "group": "长期记忆-Reranker", "description": "local 模式：已下载的模型目录绝对路径，跳过网络下载"},
        {"key": "STOCK_MEMORY_RERANKER_PRELOAD", "label": "启动预载(常驻内存)", "type": "bool", "group": "长期记忆-Reranker", "description": "仅 local 生效。true=启动时加载模型常驻(~2GB)，首次检索最快；false=按需加载、空闲卸载"},
        {"key": "STOCK_MEMORY_RERANKER_IDLE_TTL", "label": "空闲卸载秒数(0=用完即卸)", "type": "float", "group": "长期记忆-Reranker", "description": "仅 local+PRELOAD=false 生效。空闲超时自动释放 ~2GB 内存"},
        {"key": "MONITOR_INTERVAL", "label": "监控采集间隔(秒)", "type": "int", "group": "系统监控"},
        {"key": "DASHBOARD_ENABLED", "label": "启用决策仪表盘", "type": "bool", "group": "报告"},
        {"key": "DASHBOARD_LLM_ENABLED", "label": "启用仪表盘 LLM 提炼", "type": "bool", "group": "报告"},
        {"key": "DASHBOARD_MAX_LLM_TOKENS", "label": "仪表盘 LLM Token 上限", "type": "int", "group": "报告"},
        {"key": "CACHE_MAX_ENTRIES", "label": "缓存最大条目数", "type": "int", "group": "缓存"},
        {"key": "CACHE_FILE_MAX_MB", "label": "缓存文件最大大小(MB)", "type": "int", "group": "缓存"},
        {"key": "BOCHA_API_KEYS", "label": "博查 API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "BRAVE_API_KEYS", "label": "Brave API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "ANSPIRE_API_KEYS", "label": "Anspire API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "MINIMAX_API_KEYS", "label": "MiniMax API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "SEARXNG_URLS", "label": "SearXNG 实例地址", "type": "str", "group": "搜索引擎"},
        {"key": "SEARXNG_PUBLIC_INSTANCES", "label": "SearXNG 公共实例", "type": "str", "group": "搜索引擎"},
        {"key": "NOTIFICATION_ENABLED", "label": "启用通知层", "type": "bool", "group": "通知"},
        {"key": "NOTIFICATION_CHANNELS", "label": "指定渠道", "type": "str", "group": "通知"},
        {"key": "NOTIFICATION_DEDUP_TTL", "label": "去重窗口(秒)", "type": "int", "group": "通知"},
        {"key": "NOTIFICATION_COOLDOWN", "label": "最小发送间隔(秒)", "type": "int", "group": "通知"},
        {"key": "NOTIFICATION_QUIET_START", "label": "静默开始(小时)", "type": "int", "group": "通知"},
        {"key": "NOTIFICATION_QUIET_END", "label": "静默结束(小时)", "type": "int", "group": "通知"},
        {"key": "DINGTALK_WEBHOOK_URL", "label": "钉钉 Webhook URL", "type": "str", "group": "通知"},
        {"key": "DINGTALK_WEBHOOK_SECRET", "label": "钉钉签名密钥", "type": "str", "group": "通知", "sensitive": True},
        {"key": "FEISHU_WEBHOOK_URL", "label": "飞书 Webhook URL", "type": "str", "group": "通知"},
        {"key": "FEISHU_WEBHOOK_SECRET", "label": "飞书签名密钥", "type": "str", "group": "通知", "sensitive": True},
        {"key": "WECHAT_WEBHOOK_URL", "label": "企微 Webhook URL", "type": "str", "group": "通知"},
        {"key": "TELEGRAM_BOT_TOKEN", "label": "Telegram Bot Token", "type": "str", "group": "通知", "sensitive": True},
        {"key": "TELEGRAM_CHAT_ID", "label": "Telegram Chat ID", "type": "str", "group": "通知"},
        {"key": "EMAIL_SMTP_HOST", "label": "SMTP 服务器", "type": "str", "group": "通知"},
        {"key": "EMAIL_SMTP_PORT", "label": "SMTP 端口", "type": "int", "group": "通知"},
        {"key": "EMAIL_SMTP_USER", "label": "SMTP 用户名", "type": "str", "group": "通知"},
        {"key": "EMAIL_SMTP_PASSWORD", "label": "SMTP 密码", "type": "str", "group": "通知", "sensitive": True},
        {"key": "EMAIL_RECIPIENTS", "label": "邮件收件人", "type": "str", "group": "通知"},
        {"key": "CUSTOM_WEBHOOK_URL", "label": "自定义 Webhook URL", "type": "str", "group": "通知"},
        {"key": "DISCORD_WEBHOOK_URL", "label": "Discord Webhook URL", "type": "str", "group": "通知"},
        {"key": "SLACK_WEBHOOK_URL", "label": "Slack Webhook URL", "type": "str", "group": "通知"},
        {"key": "PUSHOVER_USER_KEY", "label": "Pushover User Key", "type": "str", "group": "通知", "sensitive": True},
        {"key": "PUSHOVER_APP_TOKEN", "label": "Pushover App Token", "type": "str", "group": "通知", "sensitive": True},
        {"key": "NTFY_URL", "label": "ntfy 服务器地址", "type": "str", "group": "通知"},
        {"key": "NTFY_TOPIC", "label": "ntfy 订阅主题", "type": "str", "group": "通知"},
        {"key": "GOTIFY_URL", "label": "Gotify 服务器地址", "type": "str", "group": "通知"},
        {"key": "GOTIFY_TOKEN", "label": "Gotify Token", "type": "str", "group": "通知", "sensitive": True},
        {"key": "PUSHPLUS_TOKEN", "label": "PushPlus Token", "type": "str", "group": "通知", "sensitive": True},
        {"key": "SERVERCHAN3_SENDKEY", "label": "Server酱3 SendKey", "type": "str", "group": "通知", "sensitive": True},
    ]


def get_config_schema() -> list[dict]:
    """获取配置 schema，带缓存。"""
    if not hasattr(get_config_schema, "_cache"):
        get_config_schema._cache = _build_config_schema()
    return get_config_schema._cache

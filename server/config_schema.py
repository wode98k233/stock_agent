"""配置面板 Schema 定义和辅助函数。"""
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = {
    "TAVILY_API_KEYS", "SERPAPI_API_KEYS",
    "BOCHA_API_KEYS", "BRAVE_API_KEYS", "ANSPIRE_API_KEYS", "MINIMAX_API_KEYS",
    "DEEPSEEK_API_KEYS",
    "DINGTALK_WEBHOOK_SECRET", "FEISHU_WEBHOOK_SECRET",
    "TELEGRAM_BOT_TOKEN", "EMAIL_SMTP_PASSWORD",
    "PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN",
    "GOTIFY_TOKEN", "PUSHPLUS_TOKEN", "SERVERCHAN3_SENDKEY",
    "HITHINK_FINANCE_API_KEY",
    # Embedding / 评测等密钥：接口不返回明文、页面不可改、apply 接口拒绝写入
    "STOCK_MEMORY_API_KEY", "STOCK_MEMORY_RERANKER_API_KEY", "JUDGE_LLM_API_KEY",
    # 2026-11-14：主模型/数据源凭据纳入「只写不读」可配置
    "OPENAI_API_KEY", "REPORT_LLM_API_KEY", "TOOL_COMPRESS_LLM_API_KEY",
    "CONTEXT_SUMMARY_LLM_API_KEY",
    "MX_APIKEY", "TUSHARE_TOKEN", "IWENCAI_API_KEY", "FINNHUB_API_KEY",
    "LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN",
    "CHECKPOINT_PG_URI",
}

# 只读项：页面展示但禁止通过接口修改（改 .env 后重启）
_READONLY_KEYS = {"TRACE_DB_PATH", "WEB_HOST", "WEB_PORT", "WEB_MAX_WORKERS"}


def is_readonly(key: str) -> bool:
    return key in _READONLY_KEYS


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
        {"key": "LLM_REQUEST_TIMEOUT", "label": "LLM 请求超时(秒)", "type": "int", "group": "LLM", "description": "单次 LLM 请求超时。SDK 默认 read=600s×3 次重试=最坏 30 分钟静默挂死，收紧到 300s。"},
        {"key": "CACHE_PREFIX_CONTENT", "label": "自定义缓存前缀", "type": "str", "group": "LLM"},
        # ── A8 语义缓存（LLM 响应缓存，默认关闭灰度） ──
        {"key": "LLM_CACHE_ENABLED", "label": "启用 LLM 语义缓存", "type": "bool", "group": "LLM 缓存", "description": "命中同义同场景问题直接返回缓存响应，跳过 LLM 调用。默认关闭（灰度），关闭后行为与上线前完全一致"},
        {"key": "LLM_CACHE_TTL_MAP", "label": "TTL 分级映射(秒)", "type": "str", "group": "LLM 缓存", "description": "JSON：market=市场观点(1800)；sector_intraday=盘中板块(0=禁缓存)；sector_offhours=盘后板块(14400)；methodology=方法论(604800)；data_query=数据查询(0=禁缓存)"},
        {"key": "LLM_CACHE_CATEGORY_MAP", "label": "场景→缓存类别映射", "type": "str", "group": "LLM 缓存", "description": "JSON：sr_scenario → market/sector/methodology/data_query；未列出场景默认 sector"},
        # ── A8 模型路由（默认关闭灰度） ──
        {"key": "LLM_ROUTING_ENABLED", "label": "启用模型分档路由", "type": "bool", "group": "模型路由", "description": "按场景/标签将请求路由到 fast/standard/strong 三档模型。默认关闭（灰度）。**重启服务后生效**（路由/缓存策略在启动时装配）"},
        {"key": "LLM_ROUTING_MAP", "label": "路由映射表", "type": "str", "group": "模型路由", "description": "JSON：{\"rules\":[{\"match\":{\"scenario\":[..]/\"label\":[..]/\"purpose\":[..]},\"tier\":\"fast\"}],\"default\":\"standard\"}，按顺序首条命中生效"},
        {"key": "LLM_TIER_FAST_MODEL", "label": "fast 档模型", "type": "str", "group": "模型路由", "description": "留空=沿用该用途原配模型；凭证沿用原配置，仅换模型名"},
        {"key": "LLM_TIER_STANDARD_MODEL", "label": "standard 档模型", "type": "str", "group": "模型路由", "description": "留空=沿用该用途原配模型"},
        {"key": "LLM_TIER_STRONG_MODEL", "label": "strong 档模型", "type": "str", "group": "模型路由", "description": "留空=沿用该用途原配模型"},
        {"key": "AGENT_MODE", "label": "Agent 模式", "type": "str", "group": "Agent 通用", "choices": list(AgentFactory.list()) if _agent_factory_available() else ["react_stock"]},
        {"key": "BATCH_SIZE", "label": "批处理大小", "type": "int", "group": "Agent 通用"},
        {"key": "MEMORY_ENABLED", "label": "启用对话记忆", "type": "bool", "group": "Agent 通用", "description": "控制 ConversationSummaryBufferMemory 开关"},
        {"key": "MEMORY_MAX_TOKENS", "label": "记忆压缩 Token 上限", "type": "int", "group": "Agent 通用"},
        {"key": "CONTEXT_WINDOW_TOKENS", "label": "上下文窗口上限(token)", "type": "int", "group": "Agent 通用",
         "description": "上下文洞察页占用率的分母。按模型区分请用下一项。"},
        {"key": "CONTEXT_WINDOW_BY_MODEL", "label": "按模型的窗口上限(JSON)", "type": "str", "group": "Agent 通用",
         "description": "JSON，形如 {\"deepseek-v4.1-flash\": 1000000}；命中优先于上一项。"},
        {"key": "INFO_ACCUMULATOR_COMPRESS_THRESHOLD", "label": "信息累积压缩阈值", "type": "int", "group": "Agent 通用"},
        {"key": "CLARIFY_ENABLED", "label": "启用澄清反问", "type": "bool", "group": "Agent 通用", "description": "classify 后信息不足时先向用户提问（Web 交互，CLI/TUI 暂跳过）"},
        {"key": "CLARIFY_MAX_QUESTIONS", "label": "澄清问题数量上限", "type": "int", "group": "Agent 通用"},
        {"key": "CLARIFY_TIMEOUT_SECONDS", "label": "澄清等待超时(秒)", "type": "int", "group": "Agent 通用", "description": "倒计时归零后按推荐选项继续分析"},
        {"key": "STRATEGY_INTENT_MODE", "label": "策略会话闸门", "type": "select", "choices": ["confirm", "auto", "off"], "group": "Agent 通用", "description": "对话里认出策略意图后：confirm 先弹确认卡（推荐）/ auto 槽位齐全直接跑 / off 关闭"},
        {"key": "STRATEGY_SESSION_MAX_TURNS", "label": "策略会话轮数上限", "type": "int", "group": "Agent 通用"},
        {"key": "STRATEGY_INTENT_LLM_FALLBACK", "label": "策略意图 LLM 兜底", "type": "bool", "group": "Agent 通用", "description": "词表判不了时交 LLM 裁决归属；关闭则回退「只是问问/要回测」分流题，零 LLM 调用"},
        {"key": "STRATEGY_SLOT_CARD_LLM", "label": "策略确认卡 LLM 生成", "type": "bool", "group": "Agent 通用", "description": "确认卡（反问）的题面与选项由 LLM 就着用户原话现写；关闭则用内置模板兜底"},
        {"key": "STRATEGY_INTENT_FIDELITY", "label": "策略意图保真核对", "type": "bool", "group": "Agent 通用", "description": "每轮生成后先核对「实现出来的策略」是否忠实于用户原话，再回测；关闭 = 不核对（结果卡如实标「未核对」）"},
        {"key": "REACT_TOOL_CALLS", "label": "工具调用上限", "type": "int", "group": "ReAct"},
        {"key": "REACT_SKILL_BATCH_SIZE", "label": "技能批量装载数", "type": "int", "group": "ReAct"},
        {"key": "REACT_SKILL_RESELECT_EVERY", "label": "每N轮换批(0关)", "type": "int", "group": "ReAct"},
        # 2026-10-17 删档：TOOL_SURFACE_MODE / SKILL_ACTIVATE_TOOL_MODE 已固化
        # 为常量（config/agent.py），不再进配置面板。
        {"key": "REACT_ENABLE_CONTEXT_COMPACTION", "label": "上下文压缩", "type": "bool", "group": "上下文压缩"},
        {"key": "REACT_CONTEXT_RECENT_ROUNDS", "label": "保留完整轮次", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_SUMMARY_TRIGGER_ROUNDS", "label": "摘要触发轮次", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_SUMMARY_PENDING_CHARS", "label": "摘要触发字符", "type": "int", "group": "上下文压缩"},
        {"key": "REACT_CONTEXT_SOFT_LIMIT_TOKENS", "label": "上下文软上限(tok, 0=关)",
         "type": "int", "group": "上下文压缩"},
        # 2026-10-17 删档：CONTEXT_SUMMARY_MODE 固化 fork、SKILL_SELECTOR_PROMPT_MODE
        # 固化 compact、SKILL_SELECTOR_FORK / SKILL_ROUTER_SKIP_FIRST / SKILL_DETAIL_DELIVERY
        # 同批固化（config/agent.py、config/side_llm.py），不再进配置面板。
        {"key": "CONTEXT_SUMMARY_LLM_MODEL", "label": "压缩专用模型(留空=主模型)",
         "type": "str", "group": "上下文压缩"},
        {"key": "CONTEXT_SUMMARY_LLM_API_BASE", "label": "压缩专用 API Base",
         "type": "str", "group": "上下文压缩"},
        {"key": "REACT_SUMMARY_MAX_CHARS", "label": "摘要最大字符", "type": "int", "group": "上下文压缩"},
        {"key": "BUDGET_CACHE_HIT_WEIGHT", "label": "命中 token 计费权重(0=不折算)",
         "type": "float", "group": "预算"},
        {"key": "BUDGET_FREE_WEIGHT", "label": "免费端点计费权重(0=不占预算)",
         "type": "float", "group": "预算"},
        {"key": "MAX_RAW_TOKENS_PER_QUERY", "label": "原始 token 硬上限(0=不启用)",
         "type": "int", "group": "预算"},
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
        {"key": "HITHINK_FINANCE_API_KEY", "label": "同花顺 API Key", "type": "str", "group": "数据源", "sensitive": True, "description": "同花顺官方金融数据服务 Key（fuyao.aicubes.cn/admin/ 签发），留空禁用该源"},
        {"key": "HITHINK_PRIORITY", "label": "同花顺优先级", "type": "int", "group": "数据源"},
        {"key": "REPORT_ENABLE_ANALYSIS_ENGINE", "label": "启用分析引擎", "type": "bool", "group": "报告"},
        {"key": "DEEP_REPORT_SSE_MAX_SECONDS", "label": "长报告 SSE 兜底时长(秒)", "type": "int", "group": "报告", "description": "只兜任务卡死；终态会提前收流。默认 1800（30 分钟）"},
        {"key": "REPORT_TEMPLATE", "label": "报告模板", "type": "str", "group": "报告", "choices": ["lightweight", "standard", "professional"]},
        {"key": "REPORT_TEMPLATE_CONTINUATION_MODE", "label": "模板续问判定模式", "type": "str", "group": "报告", "choices": ["keyword", "llm"], "description": "keyword=基于历史的规则续问(零成本,默认)；llm=调用 LLM 判定续问并选模板(更准,有单次调用成本)"},
        {"key": "REPORT_MODE", "label": "报告合成模式", "type": "str", "group": "报告", "choices": ["fast", "full"]},
        {"key": "REPORT_TIME_HORIZON", "label": "时间框架", "type": "str", "group": "报告", "choices": ["short", "mid", "long"]},
        {"key": "REPORT_MIN_TOKENS", "label": "报告最低 Token", "type": "int", "group": "报告"},
        {"key": "REPORT_TECH_PANEL_MAX", "label": "技术面图板上限", "type": "int", "group": "报告", "description": "报告末尾注入的 K 线技术面图板数量上限，板块分析涉及多只个股时每只各一张，0=不注入"},
        {"key": "REPORT_MAX_TOOL_OUTPUT_CHARS", "label": "工具输出截断(字符)", "type": "int", "group": "报告"},
        {"key": "REPORT_MAX_NEWS_CHARS", "label": "新闻截断(字符)", "type": "int", "group": "报告"},
        {"key": "REPORT_LLM_MODEL", "label": "报告专用模型", "type": "str", "group": "报告"},
        {"key": "REPORT_LLM_API_BASE", "label": "报告专用 API Base", "type": "str", "group": "报告"},
        {"key": "TOOL_COMPRESS_LLM_MODEL", "label": "工具压缩模型", "type": "str", "group": "报告"},
        {"key": "TOOL_COMPRESS_LLM_API_BASE", "label": "工具压缩 API Base", "type": "str", "group": "报告"},
        {"key": "JUDGE_LLM_MODEL", "label": "评测 Judge 模型", "type": "str", "group": "报告"},
        {"key": "JUDGE_LLM_API_KEY", "label": "Judge 专用 API Key", "type": "str", "group": "报告", "sensitive": True},
        {"key": "JUDGE_LLM_API_BASE", "label": "Judge 专用 API Base", "type": "str", "group": "报告"},
        {"key": "TOOL_OUTPUT_COMPRESS_THRESHOLD", "label": "工具输出压缩阈值(字符)", "type": "int", "group": "报告"},
        {"key": "TOOL_COMPRESS_THRESHOLDS", "label": "Per-tool 压缩阈值 (JSON)", "type": "str", "group": "报告"},
        {"key": "TOOL_HARD_TRUNCATE_CHARS", "label": "工具输出硬截断上限(字符)", "type": "int", "group": "报告"},
        {"key": "TOOL_RULE_COMPRESS_ENABLED", "label": "工具规则压缩开关", "type": "bool", "group": "报告", "description": "默认关闭；开启后按 TOOL_RULE_COMPRESS_POLICY 逐工具灰度"},
        {"key": "TOOL_RULE_COMPRESS_POLICY", "label": "工具规则压缩策略 (JSON: off/shadow/on)", "type": "str", "group": "报告", "description": "示例：{\"default\":\"off\",\"mx_xuangu_filter\":\"shadow\"}；shadow=双算落盘不改输出，on=采用规则输出"},
        {"key": "LOG_LEVEL", "label": "日志级别", "type": "str", "group": "调试", "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]},
        {"key": "DEBUG_STEP_CONFIRM", "label": "调试步骤确认", "type": "bool", "group": "调试"},
        {"key": "ENABLE_TRACE", "label": "启用追踪", "type": "bool", "group": "调试"},
        {"key": "WEB_DEV_RELOAD", "label": "开发热重载", "type": "bool", "group": "调试"},
        {"key": "WEB_SHOW_TRACE_TOOLS", "label": "显示溯源工具按钮", "type": "bool", "group": "调试", "description": "侧边栏「日志关联」入口（回答溯源·执行归因面板），默认隐藏；面向开发者调试用"},
        {"key": "WEB_DIALOG_AUTO_TITLE", "label": "LLM 生成会话标题", "type": "bool", "group": "调试", "description": "首轮提问时由 classify 顺带生成会话标题并回填（仅生成一次，成功后不覆盖）；关闭则沿用提问截断标题"},
        # ── 长期记忆 — 基础（始终生效，管控成本低） ──
        {"key": "JIEBA_ENABLED", "label": "jieba 中文分词", "type": "bool", "group": "长期记忆-基础", "description": "禁用节省 ~40MB，中文分词退化为逐字匹配"},
        {"key": "STOCK_MEMORY_BACKEND", "label": "检索后端", "type": "str", "group": "长期记忆-基础", "choices": ["auto", "fts5", "embedding", "hybrid"], "description": "auto=有 Embedding 则混合，否则纯文本。fts5 模式完全不需要 Embedding"},
        {"key": "MEMORY_RETRIEVAL_TOP_K", "label": "检索返回条数", "type": "int", "group": "长期记忆-基础"},
        {"key": "MEMORY_MAX_PROMPT_TOKENS", "label": "注入 Prompt 预算(Token，1 Token≈2 字符)", "type": "int", "group": "长期记忆-基础", "description": "兼容旧口径；有 MEMORY_MAX_PROMPT_CHARS 时以后者为准"},
        {"key": "MEMORY_MAX_PROMPT_CHARS", "label": "注入字符预算", "type": "int", "group": "长期记忆-基础", "description": "优先于 Token 预算；约 2 字符=1 Token"},
        {"key": "MEMORY_MAX_CHUNKS_PER_GROUP", "label": "同一报告最多注入块数", "type": "int", "group": "长期记忆-基础", "description": "旧硬编码为 3"},
        {"key": "MEMORY_CHUNK_MAX_CHARS", "label": "单块字符上限", "type": "int", "group": "长期记忆-基础", "description": "chunk 本身 ≤1000，一般无需再截"},
        {"key": "MEMORY_MIN_SIMILARITY", "label": "语义相似度下限(floor)", "type": "float", "group": "长期记忆-基础", "description": "自适应门槛的绝对下限；0=关闭"},
        {"key": "MEMORY_SIMILARITY_MARGIN", "label": "语义相似度相对带宽", "type": "float", "group": "长期记忆-基础", "description": "保留 top_score-margin 以内的命中；0=纯绝对门槛"},
        {"key": "MEMORY_PROFILE_SEMANTIC", "label": "用户画像语义抽取", "type": "bool", "group": "长期记忆-基础", "description": "prototype 向量补充关键词表；开启后每轮可能多 1 次 embedding"},
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
        {"key": "MARKET_MONITOR_ENABLED", "label": "启用盯盘告警", "type": "bool", "group": "系统监控", "description": "开启后交易时段自动轮询自选股，规则命中经通知渠道推送。需重启 Web 服务生效"},
        {"key": "MARKET_MONITOR_INTERVAL_S", "label": "盯盘轮询间隔(秒)", "type": "int", "group": "系统监控", "description": "新浪批量报价通道，60s 压力极小。需重启 Web 服务生效"},
        {"key": "MARKET_DASHBOARD_ENABLED", "label": "启用市场情绪面板", "type": "bool", "group": "市场面板", "description": "右栏温度计+异动快报总开关；关闭后前端显示已关闭且不拉取上游"},
        {"key": "MARKET_DASHBOARD_REFRESH_S", "label": "前端轮询间隔(秒)", "type": "int", "group": "市场面板", "description": "异动快报刷新间隔；情绪仍按服务端 60s 缓存"},
        {"key": "MARKET_DASHBOARD_ALERT_LIMIT", "label": "异动条数上限", "type": "int", "group": "市场面板"},
        {"key": "MARKET_DASHBOARD_NEWS_LIMIT", "label": "新闻条数上限", "type": "int", "group": "市场面板"},
        # ── 全局总览大屏（研究分析 → 全局总览） ──
        {"key": "OVERVIEW_ENABLED", "label": "启用全局总览", "type": "bool", "group": "全局总览"},
        {"key": "OVERVIEW_BLOCK_CACHE_TTL_S", "label": "研究块缓存(秒)", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_MARKET_REFRESH_S", "label": "行情块刷新(秒,0=关)", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_RUNTIME_REFRESH_S", "label": "运行状态刷新(秒)", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_SCAN_TTL_S", "label": "策略扫描缓存(秒)", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_SCAN_DAYS", "label": "策略扫描回溯(根)", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_SCAN_STRATEGIES", "label": "策略白名单(逗号分隔)", "type": "str", "group": "全局总览"},
        {"key": "OVERVIEW_SCAN_REFRESH_MAX", "label": "扫描前补尾上限(码)", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_SCAN_REFRESH_BUDGET_S", "label": "扫描前补尾预算(秒)", "type": "float", "group": "全局总览", "description": "补尾是网络密集串行操作，必须远小于扫描块软超时(90s)，否则块先判超时、worker 仍在跑、进度迟到且结果被丢弃"},
        {"key": "OVERVIEW_MACRO_INDICATORS", "label": "宏观指标白名单(逗号分隔)", "type": "str", "group": "全局总览"},
        {"key": "OVERVIEW_WATCHLIST_BUDGET_S", "label": "自选行情预算(秒)", "type": "float", "group": "全局总览"},
        {"key": "OVERVIEW_TOKEN_DAYS", "label": "Token 统计天数", "type": "int", "group": "全局总览"},
        {"key": "OVERVIEW_TOKEN_MODEL", "label": "成本估算模型名", "type": "str", "group": "全局总览", "description": "仅用于展示口径标注，不参与计算"},
        {"key": "OVERVIEW_TOKEN_PRICE_IN", "label": "输入单价(元/百万token)", "type": "float", "group": "全局总览", "description": "默认按 DeepSeek v4.1 Flash：输入 2 元 / 输出 8 元 / 缓存命中 0.2 元（每百万 token）；按实际供应商账单手工填写"},
        {"key": "OVERVIEW_TOKEN_PRICE_OUT", "label": "输出单价(元/百万token)", "type": "float", "group": "全局总览"},
        {"key": "OVERVIEW_TOKEN_PRICE_CACHED", "label": "缓存命中单价(元/百万token)", "type": "float", "group": "全局总览"},
        # ── 后台巡检（启动后异步跑，不阻塞启动；总开关关闭即完全不起调度） ──
        {"key": "BACKGROUND_SCANS_ENABLED", "label": "▶ 启用后台巡检", "type": "bool", "group": "后台巡检", "description": "总开关：台账刷新 / 存储监控扫描 / 拥挤度扫描 / 短线情绪预热。启动只注册 task 并延迟错峰执行，不影响首字节；需重启 Web 服务生效"},
        {"key": "SCAN_STORAGE_ENABLED", "label": "启用存储监控扫描", "type": "bool", "group": "后台巡检"},
        {"key": "SCAN_STORAGE_FIRST_DELAY_S", "label": "存储扫描首次延迟(秒)", "type": "int", "group": "后台巡检", "description": "错峰用：启动期路由导入与 warmup 在抢 GIL，立即开跑会拖慢首字节"},
        {"key": "SCAN_STORAGE_INTERVAL_MINUTES", "label": "存储扫描间隔(分)", "type": "int", "group": "后台巡检"},
        {"key": "SCAN_THESIS_ENABLED", "label": "启用研究台账刷新", "type": "bool", "group": "后台巡检"},
        {"key": "SCAN_THESIS_FIRST_DELAY_S", "label": "台账刷新首次延迟(秒)", "type": "int", "group": "后台巡检", "description": "首次运行会触发当日补数（宏观/指数日线），启动后约 2-3 分钟内台账就绪"},
        {"key": "SCAN_THESIS_INTERVAL_MINUTES", "label": "台账刷新间隔(分)", "type": "int", "group": "后台巡检"},
        {"key": "SCAN_CROWDING_ENABLED", "label": "启用拥挤度扫描", "type": "bool", "group": "后台巡检"},
        {"key": "SCAN_CROWDING_FIRST_DELAY_S", "label": "拥挤度首次延迟(秒)", "type": "int", "group": "后台巡检"},
        {"key": "SCAN_CROWDING_INTERVAL_MINUTES", "label": "拥挤度扫描间隔(分)", "type": "int", "group": "后台巡检", "description": "板块池有几百个标的、上游每次调用限流 2–5s，靠游标分批推进；建议保持日频"},
        {"key": "SCAN_SHORTLINE_ENABLED", "label": "启用短线情绪预热", "type": "bool", "group": "后台巡检"},
        {"key": "SCAN_SHORTLINE_FIRST_DELAY_S", "label": "短线情绪首次延迟(秒)", "type": "int", "group": "后台巡检", "description": "启动后自动拉一次当日四池/天梯并固化快照，免得打开页面现等；hithink 限频敏感，排在拥挤度之后错峰"},
        {"key": "SCAN_SHORTLINE_INTERVAL_MINUTES", "label": "短线情绪间隔(分)", "type": "int", "group": "后台巡检", "description": "盘中滚动固化当日快照（按口径日幂等 upsert），默认 60 分钟"},
        {"key": "SCAN_KLINE_ENABLED", "label": "启用 K 线保活", "type": "bool", "group": "后台巡检", "description": "把「持仓 ∪ 自选」的日线补到最近已完成交易日。关掉后 K 线只在打开页面时由扫描路径兜底补（收敛很慢），大屏策略信号会大面积标注「数据截止 N 天前」"},
        {"key": "SCAN_KLINE_FIRST_DELAY_S", "label": "K线保活首次延迟(秒)", "type": "int", "group": "后台巡检", "description": "网络密集，排在其它作业之后错峰，默认 240 秒"},
        {"key": "SCAN_KLINE_INTERVAL_MINUTES", "label": "K线保活间隔(分)", "type": "int", "group": "后台巡检", "description": "默认 60 分钟。已是最新的标的会被跳过、几乎不耗时间，故可高频"},
        {"key": "SCAN_KLINE_BUDGET_S", "label": "K线保活单轮预算(秒)", "type": "int", "group": "后台巡检", "description": "单轮最多跑多久，超时把剩余标的顺延到下一轮（避免长时间占用网络与限流额度）"},
        {"key": "SCAN_KLINE_MAX_CODES", "label": "K线保活单轮上限(码)", "type": "int", "group": "后台巡检"},
        {"key": "DASHBOARD_ENABLED", "label": "启用决策仪表盘", "type": "bool", "group": "报告"},
        {"key": "DASHBOARD_LLM_ENABLED", "label": "启用仪表盘 LLM 提炼", "type": "bool", "group": "报告"},
        {"key": "DASHBOARD_MAX_LLM_TOKENS", "label": "仪表盘 LLM Token 上限", "type": "int", "group": "报告"},
        {"key": "CACHE_MAX_ENTRIES", "label": "缓存最大条目数", "type": "int", "group": "缓存"},
        {"key": "CACHE_FILE_MAX_MB", "label": "缓存文件最大大小(MB)", "type": "int", "group": "缓存"},
        {"key": "TAVILY_API_KEYS", "label": "Tavily API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "SERPAPI_API_KEYS", "label": "SerpAPI API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "BOCHA_API_KEYS", "label": "博查 API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "BRAVE_API_KEYS", "label": "Brave API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "ANSPIRE_API_KEYS", "label": "Anspire API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "MINIMAX_API_KEYS", "label": "MiniMax API Keys", "type": "str", "group": "搜索引擎", "sensitive": True},
        {"key": "SEARXNG_URLS", "label": "SearXNG 实例地址", "type": "str", "group": "搜索引擎"},
        {"key": "SEARXNG_PUBLIC_INSTANCES", "label": "SearXNG 公共实例", "type": "str", "group": "搜索引擎"},
        {"key": "SEARCH_FREE_MCP_ENABLED", "label": "启用免 Key 境外搜索源（Parallel/Exa）", "type": "str", "group": "搜索引擎",
         "choices": ["true", "false"],
         "description": "Parallel / Exa 托管 MCP，免 Key、国内可达，作为兜底搜索源。关闭后查询不会发往境外服务。"},
        {"key": "DEEPSEEK_API_KEYS", "label": "DeepSeek API Keys（Search/Fetch）", "type": "str", "group": "搜索引擎", "sensitive": True,
         "description": "官方 Search/Fetch 专用 Key（platform.deepseek.com 申请，逗号分隔可配多个）。按请求次数计费 ¥5/1000 次成功请求，只有 200 且拿到结果才计费；LLM 走第三方中继时那把 Key 在官方站点无效，需单独配置。"},
        {"key": "DEEPSEEK_API_BASE", "label": "DeepSeek API Base", "type": "str", "group": "搜索引擎",
         "description": "默认 https://api.deepseek.com，一般无需修改。"},
        {"key": "DEEPSEEK_SEARCH_ENABLED", "label": "启用 DeepSeek 搜索源（排首位）", "type": "str", "group": "搜索引擎",
         "choices": ["true", "false"],
         "description": "启用后排在搜索链首位（自带排序分/权威度与 600~800 字符长摘要）。关闭 = 搜索不再用该源（不产生搜索费用），但 web_fetch 抓取仍可用；要一并停用请清空 DEEPSEEK_API_KEYS（Key 是总开关）。"},
        {"key": "DEEPSEEK_SEARCH_NEED_CONTENT", "label": "DeepSeek 搜索直接返回正文", "type": "str", "group": "搜索引擎",
         "choices": ["true", "false"],
         "description": "默认 false：正文交给 web_fetch 按需抓取，搜索只取长摘要，避免一次塞爆上下文。"},
        {"key": "DEEPSEEK_FETCH_MAX_CHARS", "label": "网页抓取正文上限(字符)", "type": "int", "group": "搜索引擎",
         "description": "web_fetch 单篇正文截断上限，默认 8000，实际夹在 500~20000。"},
        {"key": "SEARCH_PROVIDER_ORDER", "label": "搜索引擎顺序（白名单）", "type": "str", "group": "搜索引擎",
         "description": "逗号分隔引擎名，可用 DeepSeek/MX/Tavily/SerpAPI/Bocha/Brave/Anspire/MiniMax/Parallel/Exa/SearXNG。填了则「写了的才用、按写的顺序用」，例如 DeepSeek,MX；留空 = 默认顺序全量使用。"},
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
        {
            "key": "BACKTEST_MIN_COVERAGE",
            "label": "回测最低数据覆盖率",
            "type": "float",
            "group": "回测",
            "description": "低于该覆盖率的回测判定为数据缺口（health=data_gap），提示补数后再看结论；0.95 表示允许缺 5% 的交易日",
        },
        {
            "key": "BACKTEST_AUTO_EVAL",
            "label": "回测自动样本内外评判",
            "type": "bool",
            "group": "回测",
            "description": "回测完成后自动后台补跑 IS/OOS 对照，verdict 落 backtest_eval 表。关掉后只影响新产生的回测，已有评判结果不受影响",
        },
        {
            "key": "PRICE_LIMIT_MODE",
            "label": "涨跌停拦截口径",
            "type": "select",
            "choices": ["enforce", "warn", "off"],
            "group": "回测",
            "description": "涨停不可买 / 跌停不可卖的撮合约束：enforce=真实拦截（默认）/ warn=只记录归因但放行（量化旧曲线虚高多少）/ off=关闭（一键回滚）",
        },
        # ── 成本 / 计价（结构化价目在 data/pricing/pricing_catalog.json，用「定价目录编辑器」改） ──
        {
            "key": "COST_TRACKING_ENABLED",
            "label": "▶ 启用成本统计",
            "type": "bool",
            "group": "成本 / 计价",
            "description": "总开关：每次 LLM 调用按「实际模型 + 实际 token + 调用时刻（峰/谷）+ 供应商」算出花费，并在日志 / 单次对话 / 月度统计 / 大屏四处展示。关闭后退回只有 token 的旧口径",
        },
        {
            "key": "COST_PRICING_CATALOG_PATH",
            "label": "价目文件路径",
            "type": "str",
            "group": "成本 / 计价",
            "description": "运行时价目文件（页面可编辑）；缺失时自动回落仓库内置的 data/pricing/pricing_catalog.default.json",
        },
        {"key": "COST_CURRENCY", "label": "币种", "type": "str", "group": "成本 / 计价"},
        {"key": "COST_PEAK_TZ", "label": "峰谷判定时区", "type": "str", "group": "成本 / 计价", "description": "DeepSeek 以北京时间为准；改成 UTC 会把高峰窗口判错（09:00-12:00 会变成 01:00-04:00）"},
        {"key": "COST_UNKNOWN_PROVIDER_POLICY", "label": "未计价时的策略", "type": "str", "group": "成本 / 计价", "description": "warn=记「未计价」并告警 / zero=按 0 元计 / estimate=按最便宜付费源估"}, 
        {"key": "COST_FREE_VALIDITY_CHECK", "label": "免费源有效期检查", "type": "bool", "group": "成本 / 计价", "description": "免费额度会变：过期后自动按「未计价」处理并告警一次"}, 
        {"key": "UPLOAD_MAX_MB", "label": "单图上限 (MB)", "type": "int", "group": "上传", "description": "对话图片单张大小上限，默认 5MB"},
        {"key": "UPLOAD_RETENTION_DAYS", "label": "图片保留天数", "type": "int", "group": "上传", "description": "超过该天数的历史图片会被每日巡检清理（消息里的文字转述保留）"},
        {"key": "UPLOAD_MAX_TOTAL_MB", "label": "图片目录总量上限 (MB)", "type": "int", "group": "上传", "description": "超过后按最老文件 LRU 清理到 80%"},
        {"key": "UPLOAD_CAPTION_MAX_CHARS", "label": "图片转述字数上限", "type": "int", "group": "上传", "description": "视觉模型输出的最大字数（默认 500）；调大更详细但更慢、更贵"},

        # ── 凭据（只写不读：GET 不返回值，留空不改）──────────────
        {"key": "OPENAI_API_KEY", "label": "主模型 API Key", "type": "str", "group": "LLM", "sensitive": True, "description": "OpenAI 兼容接口的 Key；保存后立即重载"},
        {"key": "REPORT_LLM_API_KEY", "label": "报告模型 API Key", "type": "str", "group": "报告", "sensitive": True, "description": "留空 = 复用主模型 Key"},
        {"key": "TOOL_COMPRESS_LLM_API_KEY", "label": "压缩模型 API Key", "type": "str", "group": "上下文压缩", "sensitive": True, "description": "留空 = 复用主模型 Key"},
        {"key": "CONTEXT_SUMMARY_LLM_API_KEY", "label": "摘要模型 API Key", "type": "str", "group": "上下文压缩", "sensitive": True, "description": "留空 = 复用主模型 Key"},
        {"key": "MX_APIKEY", "label": "妙想 API Key", "type": "str", "group": "数据源", "sensitive": True, "description": "妙想数据源（MX）"},
        {"key": "TUSHARE_TOKEN", "label": "Tushare Token", "type": "str", "group": "数据源", "sensitive": True},
        {"key": "IWENCAI_API_KEY", "label": "问财 API Key", "type": "str", "group": "数据源", "sensitive": True},
        {"key": "IWENCAI_BASE_URL", "label": "问财 Base URL", "type": "str", "group": "数据源"},
        {"key": "FINNHUB_API_KEY", "label": "Finnhub API Key", "type": "str", "group": "数据源", "sensitive": True},
        {"key": "LONGBRIDGE_APP_KEY", "label": "Longbridge App Key", "type": "str", "group": "数据源", "sensitive": True},
        {"key": "LONGBRIDGE_APP_SECRET", "label": "Longbridge App Secret", "type": "str", "group": "数据源", "sensitive": True},
        {"key": "LONGBRIDGE_ACCESS_TOKEN", "label": "Longbridge Access Token", "type": "str", "group": "数据源", "sensitive": True},
        {"key": "CHECKPOINT_PG_URI", "label": "Checkpoint PG URI", "type": "str", "group": "调试", "sensitive": True, "description": "含口令，只写不读"},

        # ── 运行开关（此前只能在 .env 改）────────────────────────
        # 注意：TOOL_SURFACE_MODE / SKILL_ACTIVATE_TOOL_MODE / SKILL_SELECTOR_PROMPT_MODE /
        # SKILL_DETAIL_DELIVERY / SKILL_SELECTOR_FORK / SKILL_ROUTER_SKIP_FIRST /
        # CONTEXT_SUMMARY_MODE 已于 2026-10-17 固化为常量（config/agent.py、config/side_llm.py），
        # 不再是环境变量 —— 放进面板只会「改了不生效」，故不收录。
        {"key": "STRATEGY_TURN_ROUTER_LLM", "label": "策略路由用 LLM", "type": "bool", "group": "Agent 通用"},
        {"key": "GROUP_MAX_LLM_CALLS", "label": "群模式最大 LLM 调用", "type": "int", "group": "Agent 群模式"},
        {"key": "REPORT_DRILLDOWN_ENABLED", "label": "报告证据下钻", "type": "bool", "group": "报告"},
        {"key": "REPORT_DRILLDOWN_MIN_CHARS", "label": "下钻最少字符", "type": "int", "group": "报告"},
        {"key": "REPORT_DRILLDOWN_MIN_TOOLS", "label": "下钻最少工具数", "type": "int", "group": "报告"},
        {"key": "CACHE_STOCK_HISTORY_HOURS", "label": "个股历史缓存(小时)", "type": "int", "group": "缓存"},
        {"key": "CACHE_BOARD_HOURS", "label": "板块缓存(小时)", "type": "int", "group": "缓存"},
        {"key": "CACHE_NEWS_HOURS", "label": "新闻缓存(小时)", "type": "int", "group": "缓存"},
        {"key": "CACHE_RATING_HOURS", "label": "评级缓存(小时)", "type": "int", "group": "缓存"},
        {"key": "CACHE_FINANCIAL_HOURS", "label": "财务缓存(小时)", "type": "int", "group": "缓存"},
        {"key": "BACKTEST_MULTI_WINDOW_N", "label": "批量回测窗口数", "type": "int", "group": "回测"},
        {"key": "BACKTEST_MULTI_WINDOW_EMBARGO", "label": "批量回测禁运期(天)", "type": "int", "group": "回测"},
        {"key": "SCAN_MARKET_AMOUNT_ENABLED", "label": "成交额补录作业", "type": "bool", "group": "后台巡检"},
        {"key": "SCAN_MARKET_AMOUNT_INTERVAL_MINUTES", "label": "成交额补录间隔(分)", "type": "int", "group": "后台巡检"},
        {"key": "SCAN_MARKET_AMOUNT_FIRST_DELAY_S", "label": "成交额补录首延迟(秒)", "type": "int", "group": "后台巡检"},
        {"key": "DATASOURCE_PRIORITY_ORDER", "label": "数据源优先级顺序", "type": "str", "group": "数据源", "description": "逗号分隔，如 hithink,akshare,mx_data"},
        {"key": "PORTFOLIO_ENRICH_ENABLED", "label": "持仓增强取数", "type": "bool", "group": "数据源"},
        {"key": "TRACE_DB_PATH", "label": "Trace 库路径", "type": "str", "group": "调试", "readonly": True},
        {"key": "WEB_HOST", "label": "监听地址", "type": "str", "group": "系统监控", "readonly": True},
        {"key": "WEB_PORT", "label": "监听端口", "type": "int", "group": "系统监控", "readonly": True},
        {"key": "WEB_MAX_WORKERS", "label": "最大工作线程", "type": "int", "group": "系统监控", "readonly": True},
    ]


def get_config_schema() -> list[dict]:
    """获取配置 schema，带缓存。"""
    if not hasattr(get_config_schema, "_cache"):
        get_config_schema._cache = _build_config_schema()
    return get_config_schema._cache


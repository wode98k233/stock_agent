"""_ENV_MAP：reload() 的单一事实源（原 config.py 整块迁移）。

格式: 属性名 -> (环境变量名, 默认值, 类型转换函数)
新增配置在此加一行 + 对应域 mixin 加类属性（双写红线见 AGENTS.md）。
"""

_ENV_MAP = {
    # LLM
    "OPENAI_API_KEY":                   ("OPENAI_API_KEY", "", str),
    "OPENAI_API_BASE":                  ("OPENAI_API_BASE", "https://api.openai.com/v1", str),
    "OPENAI_MODEL_NAME":                ("OPENAI_MODEL_NAME", "gpt-4o", str),
    "MX_APIKEY":                        ("MX_APIKEY", "", str),
    "IWENCAI_API_KEY":                  ("IWENCAI_API_KEY", "", str),
    "IWENCAI_BASE_URL":                 ("IWENCAI_BASE_URL", "https://openapi.iwencai.com", str),
    "TUSHARE_TOKEN":                    ("TUSHARE_TOKEN", "", str),
    # 同花顺官方金融数据服务（fuyao.aicubes.cn）
    "HITHINK_FINANCE_API_KEY":          ("HITHINK_FINANCE_API_KEY", "", str),
    "HITHINK_PRIORITY":                 ("HITHINK_PRIORITY", "95", int),
    # 搜索引擎
    "SERPAPI_API_KEYS":                 ("SERPAPI_API_KEYS", "", str),
    "TAVILY_API_KEYS":                  ("TAVILY_API_KEYS", "", str),
    # 免 Key 境外托管搜索源（Parallel / Exa）总开关；关闭后查询不发往境外服务
    "SEARCH_FREE_MCP_ENABLED":          ("SEARCH_FREE_MCP_ENABLED", "true", lambda v: v.lower() == "true"),
    # DeepSeek 官方 Search / Fetch（按次计费，¥5/1000 次）；Key 需单独配（LLM 走中继时不可复用）
    "DEEPSEEK_API_KEYS":                ("DEEPSEEK_API_KEYS", "", str),
    "DEEPSEEK_API_BASE":                ("DEEPSEEK_API_BASE", "https://api.deepseek.com", str),
    "DEEPSEEK_SEARCH_ENABLED":          ("DEEPSEEK_SEARCH_ENABLED", "true", lambda v: v.lower() == "true"),
    "DEEPSEEK_SEARCH_NEED_CONTENT":     ("DEEPSEEK_SEARCH_NEED_CONTENT", "false", lambda v: v.lower() == "true"),
    "DEEPSEEK_FETCH_MAX_CHARS":         ("DEEPSEEK_FETCH_MAX_CHARS", "8000", int),
    # 搜索源顺序 / 白名单（逗号分隔引擎名；留空 = 默认顺序全量使用）
    "SEARCH_PROVIDER_ORDER":            ("SEARCH_PROVIDER_ORDER", "", str),
    # Longbridge
    "LONGBRIDGE_APP_KEY":               ("LONGBRIDGE_APP_KEY", "", str),
    "LONGBRIDGE_APP_SECRET":            ("LONGBRIDGE_APP_SECRET", "", str),
    "LONGBRIDGE_ACCESS_TOKEN":          ("LONGBRIDGE_ACCESS_TOKEN", "", str),
    # Finnhub
    "FINNHUB_API_KEY":                  ("FINNHUB_API_KEY", "", str),
    # LLM 高级配置
    "OPENAI_HEADERS":                   ("OPENAI_HEADERS", "", str),
    "OPENAI_DEFAULT_PARAMS":            ("OPENAI_DEFAULT_PARAMS", "", str),
    "OPENAI_REASONING_CONTENT_POLICY":  ("OPENAI_REASONING_CONTENT_POLICY", "auto", lambda v: v.lower()),
    "OPENAI_REASONING_EFFORT":          ("OPENAI_REASONING_EFFORT", "medium", lambda v: v.strip().lower()),
    "EXTRA_BODY":                       ("EXTRA_BODY", "", str),
    "CACHE_PREFIX_ENABLED":             ("CACHE_PREFIX_ENABLED", "false", lambda v: v.lower() == "true"),
    "LLM_REQUEST_TIMEOUT":              ("LLM_REQUEST_TIMEOUT", "300", int),
    "CACHE_PREFIX_CONTENT":             ("CACHE_PREFIX_CONTENT", "", str),
    # Agent 通用
    "BATCH_SIZE":                       ("BATCH_SIZE", "5", int),
    "MEMORY_ENABLED":                   ("MEMORY_ENABLED", "true", lambda v: v.lower() == "true"),
    "MEMORY_MAX_TOKENS":                ("MEMORY_MAX_TOKENS", "30000", int),
    # 上下文洞察：占用率的分母。CONTEXT_WINDOW_TOKENS 是兜底值，
    # CONTEXT_WINDOW_BY_MODEL 是 {"模型名": 窗口} 的 JSON，命中则优先。
    "CONTEXT_WINDOW_TOKENS":            ("CONTEXT_WINDOW_TOKENS", "1000000", int),
    "CONTEXT_WINDOW_BY_MODEL":          ("CONTEXT_WINDOW_BY_MODEL", "", str),
    # 澄清反问（Web 优先）：classify 后、正式执行前，信息不足时先问最多 5 个问题
    "CLARIFY_ENABLED":                  ("CLARIFY_ENABLED", "true", lambda v: v.lower() == "true"),
    "CLARIFY_MAX_QUESTIONS":            ("CLARIFY_MAX_QUESTIONS", "5", int),
    "CLARIFY_TIMEOUT_SECONDS":          ("CLARIFY_TIMEOUT_SECONDS", "300", int),
    # 策略会话闸门（设计文档 §5.1）：confirm 先弹确认卡 / auto 槽位齐全直接跑 / off 关闭
    "STRATEGY_INTENT_MODE":            ("STRATEGY_INTENT_MODE", "confirm", lambda v: v.strip().lower()),
    "STRATEGY_INTENT_LLM_FALLBACK":    ("STRATEGY_INTENT_LLM_FALLBACK", "true", lambda v: v.lower() == "true"),
    # 策略确认卡（反问）由 LLM 就着用户这句话现写；关掉则用内置模板兜底
    "STRATEGY_SLOT_CARD_LLM":           ("STRATEGY_SLOT_CARD_LLM", "true", lambda v: v.lower() == "true"),
    # 策略会话**轮内**阶段判定是否走 LLM（默认开）。关掉后退回「阶段默认动作」兜底，
    # 只用于离线测试与成本敏感场景；别在生产关掉 —— 那等于退回堆词猜阶段。
    "STRATEGY_TURN_ROUTER_LLM":         ("STRATEGY_TURN_ROUTER_LLM", "true", lambda v: v.lower() == "true"),
    # 策略意图保真核对：每轮生成后先核对「实现出来的策略」是否忠实于用户原话，再回测
    "STRATEGY_INTENT_FIDELITY":         ("STRATEGY_INTENT_FIDELITY", "true", lambda v: v.lower() == "true"),
    "STRATEGY_SESSION_MAX_TURNS":      ("STRATEGY_SESSION_MAX_TURNS", "12", int),
    # ReAct
    "REACT_TOOL_CALLS":                 ("REACT_TOOL_CALLS", "25", int),
    "REACT_SKILL_BATCH_SIZE":           ("REACT_SKILL_BATCH_SIZE", "5", int),
    "REACT_SKILL_RESELECT_EVERY":       ("REACT_SKILL_RESELECT_EVERY", "1", int),
    # 2026-10-17 删档：SKILL_SELECTOR_PROMPT_MODE / SKILL_DETAIL_DELIVERY /
    # SKILL_SELECTOR_FORK / SKILL_ROUTER_SKIP_FIRST / TOOL_SURFACE_MODE /
    # SKILL_ACTIVATE_TOOL_MODE 六个灰度回退档已固化为常量（config/agent.py）。
    # FR-6：预算计费口径。命中 token 按 in_hit/in_miss 折算（实测约 1/50）；
    # 0 = 不折算（还原成"命中不打折"的旧口径）。留空则从价目快照推。
    "BUDGET_CACHE_HIT_WEIGHT":         ("BUDGET_CACHE_HIT_WEIGHT", "0", float),
    # 免费端点（sensenova 等）的计费权重：0 = 完全不占预算
    "BUDGET_FREE_WEIGHT":              ("BUDGET_FREE_WEIGHT", "0", float),
    # 原始 token 硬上限（0 = 不启用）。加权配错时唯一能拦住失控的兜底。
    "MAX_RAW_TOKENS_PER_QUERY":        ("MAX_RAW_TOKENS_PER_QUERY", "0", int),
    "REACT_ENABLE_CONTEXT_COMPACTION":  ("REACT_ENABLE_CONTEXT_COMPACTION", "true", lambda v: v.lower() == "true"),
    "REACT_CONTEXT_RECENT_ROUNDS":      ("REACT_CONTEXT_RECENT_ROUNDS", "0", int),
    "REACT_SUMMARY_TRIGGER_ROUNDS":     ("REACT_SUMMARY_TRIGGER_ROUNDS", "12", int),
    "REACT_SUMMARY_PENDING_CHARS":      ("REACT_SUMMARY_PENDING_CHARS", "100000", int),
    "REACT_SUMMARY_MAX_CHARS":          ("REACT_SUMMARY_MAX_CHARS", "2500", int),
    "REACT_DEDUP_MODE":                 ("REACT_DEDUP_MODE", "exact", lambda v: v.lower()),
    "REACT_CONTEXT_DEBUG_LOG":          ("REACT_CONTEXT_DEBUG_LOG", "true", lambda v: v.lower() == "true"),
    # Plan / PDOR
    "PLAN_MAX_STEPS":                   ("PLAN_MAX_STEPS", "5", int),
    "PLAN_EXECUTOR_MAX_RETRIES":        ("PLAN_EXECUTOR_MAX_RETRIES", "3", int),
    "PLAN_EXECUTOR_TOOL_CALLS":         ("PLAN_EXECUTOR_TOOL_CALLS", "8", int),
    # Group
    "GROUP_MAX_CONCURRENT_AGENTS":      ("GROUP_MAX_CONCURRENT_AGENTS", "3", int),
    "GROUP_AGENT_MAX_RETRIES":          ("GROUP_AGENT_MAX_RETRIES", "3", int),
    "GROUP_MAX_REPLAN_COUNT":           ("GROUP_MAX_REPLAN_COUNT", "3", int),
    "GROUP_MAX_STEPS":                  ("GROUP_MAX_STEPS", "10", int),
    "GROUP_MAX_LLM_CALLS":              ("GROUP_MAX_LLM_CALLS", "100", int),
    # 预算控制
    "MAX_TOKENS_PER_QUERY":             ("MAX_TOKENS_PER_QUERY", "50000", int),
    "MAX_LLM_CALLS_PER_QUERY":          ("MAX_LLM_CALLS_PER_QUERY", "30", int),
    "MAX_TIME_SECONDS":                 ("MAX_TIME_SECONDS", "600", int),
    "BUDGET_EXEMPT_CALLS_LIMIT":        ("BUDGET_EXEMPT_CALLS_LIMIT", "10", int),
    "BUDGET_EXEMPT_TIME_LIMIT":         ("BUDGET_EXEMPT_TIME_LIMIT", "120", int),
    # 缓存
    "CACHE_EXPIRE_HOURS":               ("CACHE_EXPIRE_HOURS", "24", int),
    "CACHE_STOCK_HISTORY_HOURS":        ("CACHE_STOCK_HISTORY_HOURS", "24", int),
    "CACHE_BOARD_HOURS":                ("CACHE_BOARD_HOURS", "24", int),
    "CACHE_NEWS_HOURS":                 ("CACHE_NEWS_HOURS", "2", int),
    "CACHE_RATING_HOURS":               ("CACHE_RATING_HOURS", "72", int),
    "CACHE_FINANCIAL_HOURS":            ("CACHE_FINANCIAL_HOURS", "48", int),
    # 日志 / 调试
    "LOG_LEVEL":                        ("LOG_LEVEL", "DEBUG", lambda v: v.upper()),
    "DEBUG_STEP_CONFIRM":               ("DEBUG_STEP_CONFIRM", "true", lambda v: v.lower() == "true"),
    # Trace
    "ENABLE_TRACE":                     ("ENABLE_TRACE", "false", lambda v: v.lower() == "true"),
    "TRACE_DB_PATH":                    ("TRACE_DB_PATH", "", str),
    # Web
    "WEB_DEV_RELOAD":                   ("WEB_DEV_RELOAD", "false", lambda v: v.lower() == "true"),
    "WEB_SHOW_TRACE_TOOLS":             ("WEB_SHOW_TRACE_TOOLS", "false", lambda v: v.lower() == "true"),
    "WEB_DIALOG_AUTO_TITLE":            ("WEB_DIALOG_AUTO_TITLE", "true", lambda v: v.lower() == "true"),
    # 报告
    "REPORT_ENABLE_ANALYSIS_ENGINE":    ("REPORT_ENABLE_ANALYSIS_ENGINE", "true", lambda v: v.lower() == "true"),
    "REPORT_TEMPLATE":                  ("REPORT_TEMPLATE", "standard", str),
    "DEEP_REPORT_SSE_MAX_SECONDS":      ("DEEP_REPORT_SSE_MAX_SECONDS", "1800", int),
    "REPORT_TEMPLATE_CONTINUATION_MODE": ("REPORT_TEMPLATE_CONTINUATION_MODE", "keyword", str),
    "REPORT_TIME_HORIZON":              ("REPORT_TIME_HORIZON", "short", str),
    "REPORT_MODE":                      ("REPORT_MODE", "fast", str),
    "REPORT_MIN_TOKENS":                ("REPORT_MIN_TOKENS", "1500", int),
    "REPORT_TECH_PANEL_MAX":            ("REPORT_TECH_PANEL_MAX", "3", int),
    "REPORT_MAX_TOOL_OUTPUT_CHARS":     ("REPORT_MAX_TOOL_OUTPUT_CHARS", "5000", int),
    "REPORT_MAX_NEWS_CHARS":            ("REPORT_MAX_NEWS_CHARS", "1500", int),
    "REPORT_LLM_MODEL":                 ("REPORT_LLM_MODEL", "", str),
    "REPORT_LLM_API_KEY":              ("REPORT_LLM_API_KEY", "", str),
    "REPORT_LLM_API_BASE":             ("REPORT_LLM_API_BASE", "", str),
    # 报告证据下钻（索引→按序号选择→只载入选中工具全文；默认开启，聚焦注意力）
    "REPORT_DRILLDOWN_ENABLED":         ("REPORT_DRILLDOWN_ENABLED", "true", lambda v: v.lower() == "true"),
    "REPORT_DRILLDOWN_MIN_TOOLS":       ("REPORT_DRILLDOWN_MIN_TOOLS", "8", int),
    "REPORT_DRILLDOWN_MIN_CHARS":       ("REPORT_DRILLDOWN_MIN_CHARS", "12000", int),
    # 仪表盘
    "DASHBOARD_ENABLED":                ("DASHBOARD_ENABLED", "true", lambda v: v.lower() == "true"),
    "DASHBOARD_LLM_ENABLED":            ("DASHBOARD_LLM_ENABLED", "true", lambda v: v.lower() == "true"),
    "DASHBOARD_MAX_LLM_TOKENS":         ("DASHBOARD_MAX_LLM_TOKENS", "2000", int),
    # 压缩
    # 上下文压缩专用：与工具输出压缩拆开（PRD-A11 FR-2 用户决议）。
    # 留空 = 跟随主模型；fork 档必须同 provider，否则前缀缓存池不共享、恒不命中。
    "CONTEXT_SUMMARY_LLM_MODEL":       ("CONTEXT_SUMMARY_LLM_MODEL", "", str),
    "CONTEXT_SUMMARY_LLM_API_KEY":     ("CONTEXT_SUMMARY_LLM_API_KEY", "", str),
    "CONTEXT_SUMMARY_LLM_API_BASE":    ("CONTEXT_SUMMARY_LLM_API_BASE", "", str),
    # 默认 fork 已固化（2026-10-17 删档，见 config/side_llm.py CONTEXT_SUMMARY_MODE）。
    # 工具输出压缩专用：承接原 COMPRESS_LLM_*（sensenova 免费端点，本期行为不变）
    "TOOL_COMPRESS_LLM_MODEL":         ("TOOL_COMPRESS_LLM_MODEL", "", str),
    "TOOL_COMPRESS_LLM_API_KEY":       ("TOOL_COMPRESS_LLM_API_KEY", "", str),
    "TOOL_COMPRESS_LLM_API_BASE":      ("TOOL_COMPRESS_LLM_API_BASE", "", str),
    # 旧 COMPRESS_LLM_* 兼容别名已删（2026-10-17，发布周期已过）
    # 滑动窗口软闸门：预估 payload 超它就无条件压缩一次（PRD-A11 FR-4）
    "REACT_CONTEXT_SOFT_LIMIT_TOKENS": ("REACT_CONTEXT_SOFT_LIMIT_TOKENS", "0", int),
    # 评测 Judge 模型（建议配置与 Agent 不同的第三方模型）
    "JUDGE_LLM_MODEL":                 ("JUDGE_LLM_MODEL", "", str),
    "JUDGE_LLM_API_KEY":               ("JUDGE_LLM_API_KEY", "", str),
    "JUDGE_LLM_API_BASE":              ("JUDGE_LLM_API_BASE", "", str),
    "TOOL_OUTPUT_COMPRESS_THRESHOLD":   ("TOOL_OUTPUT_COMPRESS_THRESHOLD", "3000", int),
    "TOOL_HARD_TRUNCATE_CHARS":         ("TOOL_HARD_TRUNCATE_CHARS", "8000", int),
    "TOOL_COMPRESS_THRESHOLDS":         ("TOOL_COMPRESS_THRESHOLDS", "", str),
    "TOOL_RULE_COMPRESS_ENABLED":       ("TOOL_RULE_COMPRESS_ENABLED", "false", lambda v: v.lower() == "true"),
    "TOOL_RULE_COMPRESS_POLICY":        ("TOOL_RULE_COMPRESS_POLICY", "", str),
    "INFO_ACCUMULATOR_COMPRESS_THRESHOLD": ("INFO_ACCUMULATOR_COMPRESS_THRESHOLD", "50000", int),
    # Checkpoint
    "CHECKPOINT_BACKEND":               ("CHECKPOINT_BACKEND", "memory", str),
    "CHECKPOINT_PG_URI":                ("CHECKPOINT_PG_URI", "", str),
    # 数据源
    "DATASOURCE_MAX_FAILS":             ("DATASOURCE_MAX_FAILS", "5", int),
    # 数据源优先级自定义顺序（逗号分隔的源名，如 "hithink,akshare,mx_data"）；
    # 留空=用各源内置 priority。可在「系统配置 → 数据源优先级」里拖拽调整。
    "DATASOURCE_PRIORITY_ORDER":        ("DATASOURCE_PRIORITY_ORDER", "", str),
    "AKSHARE_ENABLE_EASTMONEY_PATCH":   ("AKSHARE_ENABLE_EASTMONEY_PATCH", "true", lambda v: v.lower() == "true"),
    # 持仓增强取数（config/agent.py 里原先只在 import 时读一次，热改不生效）
    "PORTFOLIO_ENRICH_ENABLED":         ("PORTFOLIO_ENRICH_ENABLED", "true", lambda v: v.lower() == "true"),
    # Web 服务
    "WEB_HOST":                         ("WEB_HOST", "127.0.0.1", str),
    "WEB_PORT":                         ("WEB_PORT", "8000", str),
    "WEB_MAX_WORKERS":                  ("WEB_MAX_WORKERS", "8", int),
    # 系统监控
    "MONITOR_INTERVAL":                 ("MONITOR_INTERVAL", "2", int),
    # 盯盘告警
    "MARKET_MONITOR_ENABLED":           ("MARKET_MONITOR_ENABLED", "false", lambda v: v.lower() == "true"),
    "MARKET_MONITOR_INTERVAL_S":        ("MARKET_MONITOR_INTERVAL_S", "60", int),
    "MARKET_DASHBOARD_ENABLED":         ("MARKET_DASHBOARD_ENABLED", "true", lambda v: v.lower() == "true"),
    "MARKET_DASHBOARD_REFRESH_S":       ("MARKET_DASHBOARD_REFRESH_S", "10", int),
    "MARKET_DASHBOARD_ALERT_LIMIT":     ("MARKET_DASHBOARD_ALERT_LIMIT", "30", int),
    "MARKET_DASHBOARD_NEWS_LIMIT":      ("MARKET_DASHBOARD_NEWS_LIMIT", "20", int),
    # 回测体检
    "BACKTEST_MIN_COVERAGE": ("BACKTEST_MIN_COVERAGE", "0.95", float),
    # 样本内外评判（判评）：回测完成后自动后台补跑 IS/OOS 对照
    "BACKTEST_AUTO_EVAL":               ("BACKTEST_AUTO_EVAL", "true", lambda v: v.lower() == "true"),
    # 多窗口稳健性评估（P1 §3）：K 段 + 段间 embargo（交易日）
    "BACKTEST_MULTI_WINDOW_N":          ("BACKTEST_MULTI_WINDOW_N", "4", int),
    "BACKTEST_MULTI_WINDOW_EMBARGO":    ("BACKTEST_MULTI_WINDOW_EMBARGO", "5", int),
    # 涨跌停拦截口径（P0 撮合保真）：enforce 真实拦截 / warn 只记录（量化旧曲线虚高）/ off 关闭
    "PRICE_LIMIT_MODE":                 ("PRICE_LIMIT_MODE", "enforce", lambda v: v.strip().lower()),
    # 全局总览大屏（研究分析 → 全局总览）
    "OVERVIEW_ENABLED":                 ("OVERVIEW_ENABLED", "true", lambda v: v.lower() == "true"),
    "OVERVIEW_BLOCK_CACHE_TTL_S":       ("OVERVIEW_BLOCK_CACHE_TTL_S", "60", int),
    "OVERVIEW_MARKET_REFRESH_S":        ("OVERVIEW_MARKET_REFRESH_S", "60", int),
    "OVERVIEW_RUNTIME_REFRESH_S":       ("OVERVIEW_RUNTIME_REFRESH_S", "10", int),
    "OVERVIEW_SCAN_TTL_S":              ("OVERVIEW_SCAN_TTL_S", "600", int),
    "OVERVIEW_SCAN_DAYS":               ("OVERVIEW_SCAN_DAYS", "120", int),
    "OVERVIEW_SCAN_STRATEGIES":         ("OVERVIEW_SCAN_STRATEGIES", "", str),
    "OVERVIEW_SCAN_REFRESH_BUDGET_S":   ("OVERVIEW_SCAN_REFRESH_BUDGET_S", "20", float),
    "OVERVIEW_SCAN_REFRESH_MAX":        ("OVERVIEW_SCAN_REFRESH_MAX", "6", int),
    "OVERVIEW_MACRO_INDICATORS":        ("OVERVIEW_MACRO_INDICATORS", "", str),
    "OVERVIEW_WATCHLIST_BUDGET_S":      ("OVERVIEW_WATCHLIST_BUDGET_S", "8", float),
    "OVERVIEW_TOKEN_DAYS":              ("OVERVIEW_TOKEN_DAYS", "14", int),
    "OVERVIEW_TOKEN_MODEL":             ("OVERVIEW_TOKEN_MODEL", "DeepSeek v4.1 Flash", str),
    "OVERVIEW_TOKEN_PRICE_IN":          ("OVERVIEW_TOKEN_PRICE_IN", "2.0", float),
    "OVERVIEW_TOKEN_PRICE_OUT":         ("OVERVIEW_TOKEN_PRICE_OUT", "8.0", float),
    "OVERVIEW_TOKEN_PRICE_CACHED":      ("OVERVIEW_TOKEN_PRICE_CACHED", "0.2", float),
    # 成本 / 计价（结构化价目在 data/pricing/*.json，这里只放标量开关）
    "COST_TRACKING_ENABLED":            ("COST_TRACKING_ENABLED", "true", lambda v: v.lower() == "true"),
    "COST_PRICING_CATALOG_PATH":        ("COST_PRICING_CATALOG_PATH", "data/pricing/pricing_catalog.json", str),
    "COST_CURRENCY":                    ("COST_CURRENCY", "CNY", str),
    "COST_PEAK_TZ":                     ("COST_PEAK_TZ", "Asia/Shanghai", str),
    "COST_UNKNOWN_PROVIDER_POLICY":     ("COST_UNKNOWN_PROVIDER_POLICY", "warn", str),
    "COST_FREE_VALIDITY_CHECK":         ("COST_FREE_VALIDITY_CHECK", "true", lambda v: v.lower() == "true"),
    # 后台巡检（启动后异步跑，不阻塞启动；first_delay 用于错开路由导入/warmup）
    "BACKGROUND_SCANS_ENABLED":         ("BACKGROUND_SCANS_ENABLED", "true", lambda v: v.lower() == "true"),
    "SCAN_STORAGE_ENABLED":             ("SCAN_STORAGE_ENABLED", "true", lambda v: v.lower() == "true"),
    "SCAN_STORAGE_FIRST_DELAY_S":       ("SCAN_STORAGE_FIRST_DELAY_S", "45", int),
    "SCAN_STORAGE_INTERVAL_MINUTES":    ("SCAN_STORAGE_INTERVAL_MINUTES", "720", int),
    "SCAN_THESIS_ENABLED":              ("SCAN_THESIS_ENABLED", "true", lambda v: v.lower() == "true"),
    "SCAN_THESIS_FIRST_DELAY_S":        ("SCAN_THESIS_FIRST_DELAY_S", "90", int),
    "SCAN_THESIS_INTERVAL_MINUTES":     ("SCAN_THESIS_INTERVAL_MINUTES", "240", int),
    "SCAN_CROWDING_ENABLED":            ("SCAN_CROWDING_ENABLED", "true", lambda v: v.lower() == "true"),
    "SCAN_CROWDING_FIRST_DELAY_S":      ("SCAN_CROWDING_FIRST_DELAY_S", "150", int),
    "SCAN_CROWDING_INTERVAL_MINUTES":   ("SCAN_CROWDING_INTERVAL_MINUTES", "1440", int),
    "SCAN_SHORTLINE_ENABLED":           ("SCAN_SHORTLINE_ENABLED", "true", lambda v: v.lower() == "true"),
    "SCAN_SHORTLINE_FIRST_DELAY_S":     ("SCAN_SHORTLINE_FIRST_DELAY_S", "180", int),
    "SCAN_SHORTLINE_INTERVAL_MINUTES":  ("SCAN_SHORTLINE_INTERVAL_MINUTES", "60", int),
    # K 线保活：把「持仓 ∪ 自选」的日线补到最近已完成交易日。
    # 没有它时 K 线覆盖会静默腐烂（2026-09-30 实测掉到个位数），大屏策略信号
    # 大面积「数据截止 N 天前」。首次延迟放在最后，避免启动期与其它作业抢网络。
    "SCAN_KLINE_ENABLED":               ("SCAN_KLINE_ENABLED", "true", lambda v: v.lower() == "true"),
    "SCAN_KLINE_FIRST_DELAY_S":         ("SCAN_KLINE_FIRST_DELAY_S", "240", int),
    "SCAN_KLINE_INTERVAL_MINUTES":      ("SCAN_KLINE_INTERVAL_MINUTES", "60", int),
    "SCAN_KLINE_BUDGET_S":              ("SCAN_KLINE_BUDGET_S", "120", int),
    "SCAN_KLINE_MAX_CODES":             ("SCAN_KLINE_MAX_CODES", "200", int),
    # 全市场成交额日终补录（默认关闭 = 手动工具；非交易日/收盘前自动跳过）
    "SCAN_MARKET_AMOUNT_ENABLED":       ("SCAN_MARKET_AMOUNT_ENABLED", "false", lambda v: v.lower() == "true"),
    "SCAN_MARKET_AMOUNT_FIRST_DELAY_S": ("SCAN_MARKET_AMOUNT_FIRST_DELAY_S", "3600", int),
    "SCAN_MARKET_AMOUNT_INTERVAL_MINUTES": ("SCAN_MARKET_AMOUNT_INTERVAL_MINUTES", "1440", int),
    # 记忆系统
    "JIEBA_ENABLED":                    ("JIEBA_ENABLED", "true", lambda v: v.lower() == "true"),
    "STOCK_MEMORY_BACKEND":             ("STOCK_MEMORY_BACKEND", "auto", str),
    "STOCK_MEMORY_EMBEDDING":           ("STOCK_MEMORY_EMBEDDING", "", str),
    "STOCK_MEMORY_EMBEDDING_MODEL":     ("STOCK_MEMORY_EMBEDDING_MODEL", "text-embedding-3-small", str),
    "STOCK_MEMORY_LOCAL_MODEL":         ("STOCK_MEMORY_LOCAL_MODEL", "BAAI/bge-small-zh-v1.5", str),
    "STOCK_MEMORY_API_KEY":             ("STOCK_MEMORY_API_KEY", "", str),
    "STOCK_MEMORY_API_BASE":            ("STOCK_MEMORY_API_BASE", "", str),
    "MEMORY_RETRIEVAL_TOP_K":           ("MEMORY_RETRIEVAL_TOP_K", "8", int),
    "MEMORY_MAX_PROMPT_TOKENS":         ("MEMORY_MAX_PROMPT_TOKENS", "1500", int),
    "MEMORY_MAX_PROMPT_CHARS":          ("MEMORY_MAX_PROMPT_CHARS", "6000", int),
    "MEMORY_MAX_CHUNKS_PER_GROUP":      ("MEMORY_MAX_CHUNKS_PER_GROUP", "6", int),
    "MEMORY_CHUNK_MAX_CHARS":           ("MEMORY_CHUNK_MAX_CHARS", "1000", int),
    "MEMORY_MIN_SIMILARITY":            ("MEMORY_MIN_SIMILARITY", "0.30", float),
    "MEMORY_SIMILARITY_MARGIN":         ("MEMORY_SIMILARITY_MARGIN", "0.12", float),
    "MEMORY_PROFILE_SEMANTIC":          ("MEMORY_PROFILE_SEMANTIC", "true", lambda v: v.lower() == "true"),
    "MEMORY_EPISODIC_RETENTION_DAYS":   ("MEMORY_EPISODIC_RETENTION_DAYS", "90", int),
    "MEMORY_CONFIDENCE_THRESHOLD":      ("MEMORY_CONFIDENCE_THRESHOLD", "0.4", float),
    "MEMORY_AUTO_CLEANUP_INTERVAL_MINUTES": ("MEMORY_AUTO_CLEANUP_INTERVAL_MINUTES", "60", int),
    "STOCK_MEMORY_EMBEDDING_READY_TIMEOUT": ("STOCK_MEMORY_EMBEDDING_READY_TIMEOUT", "30", float),
    "STOCK_MEMORY_EMBEDDING_READY_INTERVAL": ("STOCK_MEMORY_EMBEDDING_READY_INTERVAL", "1.5", float),
    "STOCK_MEMORY_RERANKER":            ("STOCK_MEMORY_RERANKER", "", str),
    "STOCK_MEMORY_RERANKER_MODEL":      ("STOCK_MEMORY_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3", str),
    "STOCK_MEMORY_RERANKER_LOCAL_DIR":  ("STOCK_MEMORY_RERANKER_LOCAL_DIR", "", str),
    "STOCK_MEMORY_RERANKER_API_KEY":    ("STOCK_MEMORY_RERANKER_API_KEY", "", str),
    "STOCK_MEMORY_RERANKER_API_BASE":   ("STOCK_MEMORY_RERANKER_API_BASE", "", str),
    "STOCK_MEMORY_RERANKER_IDLE_TTL":   ("STOCK_MEMORY_RERANKER_IDLE_TTL", "120", float),
    "STOCK_MEMORY_RERANKER_PRELOAD":    ("STOCK_MEMORY_RERANKER_PRELOAD", "false",
                                         lambda v: v.lower() == "true"),
    "UPLOAD_MAX_MB":                     ("UPLOAD_MAX_MB", "5", int),
    "UPLOAD_RETENTION_DAYS":             ("UPLOAD_RETENTION_DAYS", "45", int),
    "UPLOAD_MAX_TOTAL_MB":               ("UPLOAD_MAX_TOTAL_MB", "500", int),
    "UPLOAD_CAPTION_MAX_CHARS":          ("UPLOAD_CAPTION_MAX_CHARS", "500", int),
}

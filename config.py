"""
选股雷达 - 统一配置管理
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from utils.app_paths import get_db_path, get_logs_dir, get_trace_db_path

# 显式指定 .env 路径：打包态读 exe 同级目录，开发态读项目根目录
# 注意：所有类属性在 import 时就通过 os.getenv() 求值并固化。
# 这要求 load_dotenv() 必须先于任何 Config 属性访问执行。
# 当前通过 cli/bootstrap.py 和 server/bootstrap.py 的 import 顺序隐式保证。
# 如果在 load_dotenv() 之前 import 了 config 模块，所有值都会是空/默认值。
_env_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
load_dotenv(_env_dir / '.env')


# ── 可重载配置映射 ─────────────────────────────────────────────────────
# 格式: 属性名 -> (环境变量名, 默认值, 类型转换函数)
# reload() 遍历此表统一重载，新增配置只需在此添加一行
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
    "CACHE_PREFIX_CONTENT":             ("CACHE_PREFIX_CONTENT", "", str),
    # Agent 通用
    "BATCH_SIZE":                       ("BATCH_SIZE", "5", int),
    "MEMORY_ENABLED":                   ("MEMORY_ENABLED", "true", lambda v: v.lower() == "true"),
    "MEMORY_MAX_TOKENS":                ("MEMORY_MAX_TOKENS", "8000", int),
    # 澄清反问（Web 优先）：classify 后、正式执行前，信息不足时先问最多 5 个问题
    "CLARIFY_ENABLED":                  ("CLARIFY_ENABLED", "true", lambda v: v.lower() == "true"),
    "CLARIFY_MAX_QUESTIONS":            ("CLARIFY_MAX_QUESTIONS", "5", int),
    "CLARIFY_TIMEOUT_SECONDS":          ("CLARIFY_TIMEOUT_SECONDS", "300", int),
    # ReAct
    "REACT_TOOL_CALLS":                 ("REACT_TOOL_CALLS", "25", int),
    "REACT_SKILL_BATCH_SIZE":           ("REACT_SKILL_BATCH_SIZE", "5", int),
    "REACT_SKILL_RESELECT_EVERY":       ("REACT_SKILL_RESELECT_EVERY", "1", int),
    "REACT_ENABLE_CONTEXT_COMPACTION":  ("REACT_ENABLE_CONTEXT_COMPACTION", "true", lambda v: v.lower() == "true"),
    "REACT_CONTEXT_RECENT_ROUNDS":      ("REACT_CONTEXT_RECENT_ROUNDS", "0", int),
    "REACT_SUMMARY_TRIGGER_ROUNDS":     ("REACT_SUMMARY_TRIGGER_ROUNDS", "3", int),
    "REACT_SUMMARY_PENDING_CHARS":      ("REACT_SUMMARY_PENDING_CHARS", "8000", int),
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
    "REPORT_TEMPLATE_CONTINUATION_MODE": ("REPORT_TEMPLATE_CONTINUATION_MODE", "keyword", str),
    "REPORT_TIME_HORIZON":              ("REPORT_TIME_HORIZON", "short", str),
    "REPORT_MODE":                      ("REPORT_MODE", "fast", str),
    "REPORT_MIN_TOKENS":                ("REPORT_MIN_TOKENS", "3500", int),
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
    "COMPRESS_LLM_MODEL":              ("COMPRESS_LLM_MODEL", "", str),
    "COMPRESS_LLM_API_KEY":            ("COMPRESS_LLM_API_KEY", "", str),
    "COMPRESS_LLM_API_BASE":           ("COMPRESS_LLM_API_BASE", "", str),
    # 评测 Judge 模型（建议配置与 Agent 不同的第三方模型）
    "JUDGE_LLM_MODEL":                 ("JUDGE_LLM_MODEL", "", str),
    "JUDGE_LLM_API_KEY":               ("JUDGE_LLM_API_KEY", "", str),
    "JUDGE_LLM_API_BASE":              ("JUDGE_LLM_API_BASE", "", str),
    "TOOL_OUTPUT_COMPRESS_THRESHOLD":   ("TOOL_OUTPUT_COMPRESS_THRESHOLD", "3000", int),
    "TOOL_HARD_TRUNCATE_CHARS":         ("TOOL_HARD_TRUNCATE_CHARS", "8000", int),
    "TOOL_COMPRESS_THRESHOLDS":         ("TOOL_COMPRESS_THRESHOLDS", "", str),
    "TOOL_RULE_COMPRESS_ENABLED":       ("TOOL_RULE_COMPRESS_ENABLED", "false", lambda v: v.lower() == "true"),
    "TOOL_RULE_COMPRESS_POLICY":        ("TOOL_RULE_COMPRESS_POLICY", "", str),
    "INFO_ACCUMULATOR_COMPRESS_THRESHOLD": ("INFO_ACCUMULATOR_COMPRESS_THRESHOLD", "6000", int),
    # Checkpoint
    "CHECKPOINT_BACKEND":               ("CHECKPOINT_BACKEND", "memory", str),
    "CHECKPOINT_PG_URI":                ("CHECKPOINT_PG_URI", "", str),
    # 数据源
    "DATASOURCE_MAX_FAILS":             ("DATASOURCE_MAX_FAILS", "5", int),
    # 数据源优先级自定义顺序（逗号分隔的源名，如 "hithink,akshare,mx_data"）；
    # 留空=用各源内置 priority。可在「系统配置 → 数据源优先级」里拖拽调整。
    "DATASOURCE_PRIORITY_ORDER":        ("DATASOURCE_PRIORITY_ORDER", "", str),
    "AKSHARE_ENABLE_EASTMONEY_PATCH":   ("AKSHARE_ENABLE_EASTMONEY_PATCH", "true", lambda v: v.lower() == "true"),
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
    "STOCK_MEMORY_RERANKER_PRELOAD":    ("STOCK_MEMORY_RERANKER_PRELOAD", "false", str),
}


class Config:
    """统一配置，所有环境变量在这里读取"""

    # LLM
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "gpt-4o")
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

    # Longbridge 配置
    LONGBRIDGE_APP_KEY = os.getenv("LONGBRIDGE_APP_KEY", "")
    LONGBRIDGE_APP_SECRET = os.getenv("LONGBRIDGE_APP_SECRET", "")
    LONGBRIDGE_ACCESS_TOKEN = os.getenv("LONGBRIDGE_ACCESS_TOKEN", "")

    # Finnhub 配置（美股数据源）
    FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

    # LLM 高级配置
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
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
    MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
    MEMORY_MAX_TOKENS = int(os.getenv("MEMORY_MAX_TOKENS", "8000"))
    # 澄清反问（Web）：classify 后信息不足时先问最多 5 个问题；超时按推荐选项继续
    CLARIFY_ENABLED = os.getenv("CLARIFY_ENABLED", "true").lower() == "true"
    CLARIFY_MAX_QUESTIONS = int(os.getenv("CLARIFY_MAX_QUESTIONS", "5"))
    CLARIFY_TIMEOUT_SECONDS = int(os.getenv("CLARIFY_TIMEOUT_SECONDS", "300"))

    # ReAct 模式配置（LangGraph recursion_limit：每个 LLM 调用 + 每个工具调用各算 1 步）
    REACT_TOOL_CALLS = int(os.getenv("REACT_TOOL_CALLS", "25"))
    # 一次装载候选池前 N 个技能的工具+L1（<=0 表示全量；=1 即旧的单 active 行为）
    REACT_SKILL_BATCH_SIZE = int(os.getenv("REACT_SKILL_BATCH_SIZE", "5"))
    # 每 N 轮工具后回 router 重新组批（中途换批；<=0 关闭）
    REACT_SKILL_RESELECT_EVERY = int(os.getenv("REACT_SKILL_RESELECT_EVERY", "1"))
    REACT_ENABLE_CONTEXT_COMPACTION = os.getenv("REACT_ENABLE_CONTEXT_COMPACTION", "true").lower() == "true"
    REACT_CONTEXT_RECENT_ROUNDS = int(os.getenv("REACT_CONTEXT_RECENT_ROUNDS", "0"))
    REACT_SUMMARY_TRIGGER_ROUNDS = int(os.getenv("REACT_SUMMARY_TRIGGER_ROUNDS", "3"))
    REACT_SUMMARY_PENDING_CHARS = int(os.getenv("REACT_SUMMARY_PENDING_CHARS", "8000"))
    REACT_SUMMARY_MAX_CHARS = int(os.getenv("REACT_SUMMARY_MAX_CHARS", "2500"))
    REACT_DEDUP_MODE = os.getenv("REACT_DEDUP_MODE", "exact").lower()
    REACT_CONTEXT_DEBUG_LOG = os.getenv("REACT_CONTEXT_DEBUG_LOG", "true").lower() == "true"

    # Plan / PDOR / Unified 模式配置
    PLAN_MAX_STEPS = int(os.getenv("PLAN_MAX_STEPS", "5"))
    PLAN_EXECUTOR_MAX_RETRIES = int(os.getenv("PLAN_EXECUTOR_MAX_RETRIES", "3"))
    PLAN_EXECUTOR_TOOL_CALLS = int(os.getenv("PLAN_EXECUTOR_TOOL_CALLS", "8"))

    # Agent 群模式配置
    GROUP_MAX_CONCURRENT_AGENTS = int(os.getenv("GROUP_MAX_CONCURRENT_AGENTS", "3"))
    GROUP_AGENT_MAX_RETRIES = int(os.getenv("GROUP_AGENT_MAX_RETRIES", "3"))
    GROUP_MAX_REPLAN_COUNT = int(os.getenv("GROUP_MAX_REPLAN_COUNT", "3"))
    GROUP_MAX_STEPS = int(os.getenv("GROUP_MAX_STEPS", "10"))
    GROUP_MAX_LLM_CALLS = int(os.getenv("GROUP_MAX_LLM_CALLS", "100"))

    # 预算控制
    MAX_TOKENS_PER_QUERY = int(os.getenv("MAX_TOKENS_PER_QUERY", "50000"))
    MAX_LLM_CALLS_PER_QUERY = int(os.getenv("MAX_LLM_CALLS_PER_QUERY", "30"))
    MAX_TIME_SECONDS = int(os.getenv("MAX_TIME_SECONDS", "600"))
    # 豁免窗口：预算超限后用户选择继续时，短期内跳过 budget check
    BUDGET_EXEMPT_CALLS_LIMIT = int(os.getenv("BUDGET_EXEMPT_CALLS_LIMIT", "10"))
    BUDGET_EXEMPT_TIME_LIMIT = int(os.getenv("BUDGET_EXEMPT_TIME_LIMIT", "120"))

    # 缓存（小时）
    CACHE_EXPIRE_HOURS = int(os.getenv("CACHE_EXPIRE_HOURS", "24"))
    CACHE_STOCK_HISTORY_HOURS = int(os.getenv("CACHE_STOCK_HISTORY_HOURS", "24"))
    CACHE_BOARD_HOURS = int(os.getenv("CACHE_BOARD_HOURS", "24"))
    CACHE_NEWS_HOURS = int(os.getenv("CACHE_NEWS_HOURS", "2"))
    CACHE_RATING_HOURS = int(os.getenv("CACHE_RATING_HOURS", "72"))
    CACHE_FINANCIAL_HOURS = int(os.getenv("CACHE_FINANCIAL_HOURS", "48"))

    # 日志
    LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

    # 调试模式：在 DEBUG 级别下，每个 step 执行前需要手动确认
    DEBUG_STEP_CONFIRM = os.getenv("DEBUG_STEP_CONFIRM", "true").lower() == "true"

    # 调用链追踪
    ENABLE_TRACE = os.getenv("ENABLE_TRACE", "false").lower() == "true"
    TRACE_DB_PATH = os.getenv("TRACE_DB_PATH", "")

    # 开发模式：每次请求前热重载 Agent 模块（代码修改即时生效，无需重启）
    WEB_DEV_RELOAD = os.getenv("WEB_DEV_RELOAD", "false").lower() == "true"

    # 界面：侧边栏显示溯源工具（回答溯源/日志关联面板），默认隐藏（面向开发者的调试功能）
    WEB_SHOW_TRACE_TOOLS = os.getenv("WEB_SHOW_TRACE_TOOLS", "false").lower() == "true"

    # 界面：首轮对话由 classify 用 LLM 生成会话标题并回填（仅生成一次，成功后不再覆盖）
    WEB_DIALOG_AUTO_TITLE = os.getenv("WEB_DIALOG_AUTO_TITLE", "true").lower() == "true"

    # 分析框架引擎
    REPORT_ENABLE_ANALYSIS_ENGINE = os.getenv("REPORT_ENABLE_ANALYSIS_ENGINE", "true").lower() == "true"
    REPORT_TEMPLATE = os.getenv("REPORT_TEMPLATE", "standard")
    # 模板续问判定模式：keyword=基于历史的规则续问(默认,零成本)；llm=调用 LLM 判定续问并选模板
    REPORT_TEMPLATE_CONTINUATION_MODE = os.getenv("REPORT_TEMPLATE_CONTINUATION_MODE", "keyword").strip().lower()
    REPORT_TIME_HORIZON = os.getenv("REPORT_TIME_HORIZON", "short")
    REPORT_MODE = os.getenv("REPORT_MODE", "fast")
    REPORT_MIN_TOKENS = int(os.getenv("REPORT_MIN_TOKENS", "3500"))
    REPORT_TECH_PANEL_MAX = int(os.getenv("REPORT_TECH_PANEL_MAX", "3"))
    REPORT_MAX_TOOL_OUTPUT_CHARS = int(os.getenv("REPORT_MAX_TOOL_OUTPUT_CHARS", "5000"))
    REPORT_MAX_NEWS_CHARS = int(os.getenv("REPORT_MAX_NEWS_CHARS", "1500"))
    REPORT_LLM_MODEL = os.getenv("REPORT_LLM_MODEL", "")
    REPORT_LLM_API_KEY = os.getenv("REPORT_LLM_API_KEY", "")
    REPORT_LLM_API_BASE = os.getenv("REPORT_LLM_API_BASE", "")
    # 报告证据下钻：索引→按序号选择→只载入选中工具全文（默认 true，聚焦注意力）
    REPORT_DRILLDOWN_ENABLED = os.getenv("REPORT_DRILLDOWN_ENABLED", "true").lower() == "true"
    REPORT_DRILLDOWN_MIN_TOOLS = int(os.getenv("REPORT_DRILLDOWN_MIN_TOOLS", "8"))
    REPORT_DRILLDOWN_MIN_CHARS = int(os.getenv("REPORT_DRILLDOWN_MIN_CHARS", "12000"))
    DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() == "true"
    DASHBOARD_LLM_ENABLED = os.getenv("DASHBOARD_LLM_ENABLED", "true").lower() == "true"
    DASHBOARD_MAX_LLM_TOKENS = int(os.getenv("DASHBOARD_MAX_LLM_TOKENS", "2000"))
    COMPRESS_LLM_MODEL = os.getenv("COMPRESS_LLM_MODEL", "")
    COMPRESS_LLM_API_KEY = os.getenv("COMPRESS_LLM_API_KEY", "")
    COMPRESS_LLM_API_BASE = os.getenv("COMPRESS_LLM_API_BASE", "")
    # 评测 Judge 模型
    JUDGE_LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "")
    JUDGE_LLM_API_KEY = os.getenv("JUDGE_LLM_API_KEY", "")
    JUDGE_LLM_API_BASE = os.getenv("JUDGE_LLM_API_BASE", "")
    TOOL_OUTPUT_COMPRESS_THRESHOLD = int(os.getenv("TOOL_OUTPUT_COMPRESS_THRESHOLD", "3000"))
    TOOL_HARD_TRUNCATE_CHARS = int(os.getenv("TOOL_HARD_TRUNCATE_CHARS", "8000"))
    TOOL_COMPRESS_THRESHOLDS = os.getenv("TOOL_COMPRESS_THRESHOLDS", "")
    # 工具输出规则压缩（默认关闭；policy 按工具配置 off/shadow/on，例如 {"mx_xuangu_filter":"shadow"}）
    TOOL_RULE_COMPRESS_ENABLED = os.getenv("TOOL_RULE_COMPRESS_ENABLED", "false").lower() == "true"
    TOOL_RULE_COMPRESS_POLICY = os.getenv("TOOL_RULE_COMPRESS_POLICY", "")
    INFO_ACCUMULATOR_COMPRESS_THRESHOLD = int(os.getenv("INFO_ACCUMULATOR_COMPRESS_THRESHOLD", "6000"))

    # Checkpoint 后端
    CHECKPOINT_BACKEND = os.getenv("CHECKPOINT_BACKEND", "memory")
    CHECKPOINT_PG_URI = os.getenv("CHECKPOINT_PG_URI", "")

    # 东财反扒补丁
    AKSHARE_ENABLE_EASTMONEY_PATCH = os.getenv("AKSHARE_ENABLE_EASTMONEY_PATCH", "true").lower() == "true"

    # 数据库路径 - 动态获取，支持打包部署
    @classmethod
    def get_db_path(cls) -> str:
        return get_db_path()

    @classmethod
    def get_checkpoint_db_path(cls) -> str:
        from utils.app_paths import get_checkpoint_db_path
        return get_checkpoint_db_path()

    @classmethod
    def get_market_data_db_path(cls) -> str:
        from utils.app_paths import get_market_data_db_path
        return get_market_data_db_path()

    @classmethod
    def get_fundamental_db_path(cls) -> str:
        from utils.app_paths import get_fundamental_db_path
        return get_fundamental_db_path()

    @classmethod
    def get_market_cache_db_path(cls) -> str:
        from utils.app_paths import get_market_cache_db_path
        return get_market_cache_db_path()

    @classmethod
    def get_backtest_db_path(cls) -> str:
        from utils.app_paths import get_backtest_db_path
        return get_backtest_db_path()

    @classmethod
    def get_stock_memory_db_path(cls) -> str:
        from utils.app_paths import get_stock_memory_db_path
        return get_stock_memory_db_path()

    @classmethod
    def get_log_dir(cls) -> str:
        return get_logs_dir()

    # 数据源
    DATASOURCE_MAX_FAILS = int(os.getenv("DATASOURCE_MAX_FAILS", "5"))
    # 逗号分隔的数据源名顺序；留空表示用各源内置 priority
    DATASOURCE_PRIORITY_ORDER = os.getenv("DATASOURCE_PRIORITY_ORDER", "")

    # Web 服务
    WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT = os.getenv("WEB_PORT", "8000")
    WEB_MAX_WORKERS = int(os.getenv("WEB_MAX_WORKERS", "8"))

    # 系统监控
    MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "2"))

    # 盯盘告警（默认关闭：升级后行为与现状完全一致）
    MARKET_MONITOR_ENABLED = os.getenv("MARKET_MONITOR_ENABLED", "false").lower() == "true"
    MARKET_MONITOR_INTERVAL_S = int(os.getenv("MARKET_MONITOR_INTERVAL_S", "60"))

    # 市场情绪面板（方案 C：右栏温度计 + 异动快报）
    MARKET_DASHBOARD_ENABLED = os.getenv("MARKET_DASHBOARD_ENABLED", "true").lower() == "true"
    MARKET_DASHBOARD_REFRESH_S = int(os.getenv("MARKET_DASHBOARD_REFRESH_S", "10"))
    MARKET_DASHBOARD_ALERT_LIMIT = int(os.getenv("MARKET_DASHBOARD_ALERT_LIMIT", "30"))
    MARKET_DASHBOARD_NEWS_LIMIT = int(os.getenv("MARKET_DASHBOARD_NEWS_LIMIT", "20"))

    # 全局总览大屏（研究分析 → 全局总览；默认值即行为）
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

    # 后台巡检：启动后异步跑（首次延迟错开路由导入与各 warmup，避免 GIL 争抢）
    BACKGROUND_SCANS_ENABLED = os.getenv("BACKGROUND_SCANS_ENABLED", "true").lower() == "true"
    SCAN_STORAGE_ENABLED = os.getenv("SCAN_STORAGE_ENABLED", "true").lower() == "true"
    SCAN_STORAGE_FIRST_DELAY_S = int(os.getenv("SCAN_STORAGE_FIRST_DELAY_S", "45"))
    SCAN_STORAGE_INTERVAL_MINUTES = int(os.getenv("SCAN_STORAGE_INTERVAL_MINUTES", "720"))
    SCAN_THESIS_ENABLED = os.getenv("SCAN_THESIS_ENABLED", "true").lower() == "true"
    SCAN_THESIS_FIRST_DELAY_S = int(os.getenv("SCAN_THESIS_FIRST_DELAY_S", "90"))
    SCAN_THESIS_INTERVAL_MINUTES = int(os.getenv("SCAN_THESIS_INTERVAL_MINUTES", "240"))
    SCAN_CROWDING_ENABLED = os.getenv("SCAN_CROWDING_ENABLED", "true").lower() == "true"
    SCAN_CROWDING_FIRST_DELAY_S = int(os.getenv("SCAN_CROWDING_FIRST_DELAY_S", "150"))
    SCAN_CROWDING_INTERVAL_MINUTES = int(os.getenv("SCAN_CROWDING_INTERVAL_MINUTES", "1440"))
    # 短线情绪预热：启动后自动拉一次当日四池/天梯并固化快照（hithink 限频敏感，放最后错峰）
    SCAN_SHORTLINE_ENABLED = os.getenv("SCAN_SHORTLINE_ENABLED", "true").lower() == "true"
    SCAN_SHORTLINE_FIRST_DELAY_S = int(os.getenv("SCAN_SHORTLINE_FIRST_DELAY_S", "180"))
    SCAN_SHORTLINE_INTERVAL_MINUTES = int(os.getenv("SCAN_SHORTLINE_INTERVAL_MINUTES", "60"))
    SCAN_KLINE_ENABLED = os.getenv("SCAN_KLINE_ENABLED", "true").lower() == "true"
    SCAN_KLINE_FIRST_DELAY_S = int(os.getenv("SCAN_KLINE_FIRST_DELAY_S", "240"))
    SCAN_KLINE_INTERVAL_MINUTES = int(os.getenv("SCAN_KLINE_INTERVAL_MINUTES", "60"))
    SCAN_KLINE_BUDGET_S = int(os.getenv("SCAN_KLINE_BUDGET_S", "120"))
    SCAN_KLINE_MAX_CODES = int(os.getenv("SCAN_KLINE_MAX_CODES", "200"))

    # 持仓账本 agent 注入（关键词门控 + 无持仓静默跳过；关闭即回到现状）
    PORTFOLIO_ENRICH_ENABLED = os.getenv("PORTFOLIO_ENRICH_ENABLED", "true").lower() == "true"

    # 记忆系统
    JIEBA_ENABLED = os.getenv("JIEBA_ENABLED", "true").lower() == "true"
    STOCK_MEMORY_BACKEND = os.getenv("STOCK_MEMORY_BACKEND", "auto")
    STOCK_MEMORY_EMBEDDING = os.getenv("STOCK_MEMORY_EMBEDDING", "")
    STOCK_MEMORY_EMBEDDING_MODEL = os.getenv("STOCK_MEMORY_EMBEDDING_MODEL", "text-embedding-3-small")
    STOCK_MEMORY_LOCAL_MODEL = os.getenv("STOCK_MEMORY_LOCAL_MODEL", "BAAI/bge-small-zh-v1.5")
    STOCK_MEMORY_API_KEY = os.getenv("STOCK_MEMORY_API_KEY", "")
    STOCK_MEMORY_API_BASE = os.getenv("STOCK_MEMORY_API_BASE", "")
    MEMORY_RETRIEVAL_TOP_K = int(os.getenv("MEMORY_RETRIEVAL_TOP_K", "5"))
    MEMORY_MAX_PROMPT_TOKENS = int(os.getenv("MEMORY_MAX_PROMPT_TOKENS", "1500"))
    MEMORY_MAX_PROMPT_CHARS = int(os.getenv("MEMORY_MAX_PROMPT_CHARS", "6000"))
    MEMORY_MAX_CHUNKS_PER_GROUP = int(os.getenv("MEMORY_MAX_CHUNKS_PER_GROUP", "6"))
    MEMORY_CHUNK_MAX_CHARS = int(os.getenv("MEMORY_CHUNK_MAX_CHARS", "1000"))
    MEMORY_MIN_SIMILARITY = float(os.getenv("MEMORY_MIN_SIMILARITY", "0.30"))
    MEMORY_SIMILARITY_MARGIN = float(os.getenv("MEMORY_SIMILARITY_MARGIN", "0.12"))
    MEMORY_PROFILE_SEMANTIC = os.getenv("MEMORY_PROFILE_SEMANTIC", "true").lower() == "true"
    MEMORY_EPISODIC_RETENTION_DAYS = int(os.getenv("MEMORY_EPISODIC_RETENTION_DAYS", "90"))
    MEMORY_CONFIDENCE_THRESHOLD = float(os.getenv("MEMORY_CONFIDENCE_THRESHOLD", "0.4"))
    MEMORY_AUTO_CLEANUP_INTERVAL_MINUTES = int(os.getenv("MEMORY_AUTO_CLEANUP_INTERVAL_MINUTES", "60"))
    STOCK_MEMORY_EMBEDDING_READY_TIMEOUT = float(os.getenv("STOCK_MEMORY_EMBEDDING_READY_TIMEOUT", "30"))
    STOCK_MEMORY_EMBEDDING_READY_INTERVAL = float(os.getenv("STOCK_MEMORY_EMBEDDING_READY_INTERVAL", "1.5"))
    STOCK_MEMORY_RERANKER = os.getenv("STOCK_MEMORY_RERANKER", "")
    STOCK_MEMORY_RERANKER_MODEL = os.getenv("STOCK_MEMORY_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    STOCK_MEMORY_RERANKER_LOCAL_DIR = os.getenv("STOCK_MEMORY_RERANKER_LOCAL_DIR", "")
    STOCK_MEMORY_RERANKER_API_KEY = os.getenv("STOCK_MEMORY_RERANKER_API_KEY", "")
    STOCK_MEMORY_RERANKER_API_BASE = os.getenv("STOCK_MEMORY_RERANKER_API_BASE", "")
    STOCK_MEMORY_RERANKER_IDLE_TTL = float(os.getenv("STOCK_MEMORY_RERANKER_IDLE_TTL", "120"))
    STOCK_MEMORY_RERANKER_PRELOAD = os.getenv("STOCK_MEMORY_RERANKER_PRELOAD", "false").lower() == "true"

    # ── 动态配置更新 ──

    @classmethod
    def set_log_level(cls, level: str):
        level = level.upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"无效的日志级别: {level}")
        cls.LOG_LEVEL = level

    @classmethod
    def set_debug_step_confirm(cls, enabled: bool):
        cls.DEBUG_STEP_CONFIRM = enabled

    @classmethod
    def get_tool_compress_threshold(cls, tool_name: str) -> int:
        """获取指定工具的压缩阈值，优先使用 TOOL_COMPRESS_THRESHOLDS JSON 配置"""
        if cls.TOOL_COMPRESS_THRESHOLDS:
            try:
                import json
                thresholds = json.loads(cls.TOOL_COMPRESS_THRESHOLDS)
                return int(thresholds.get(tool_name, thresholds.get("default", cls.TOOL_OUTPUT_COMPRESS_THRESHOLD)))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return cls.TOOL_OUTPUT_COMPRESS_THRESHOLD

    @classmethod
    def reload(cls) -> set:
        """重新从 .env 加载并更新所有类属性，返回变更的 key 集合"""
        from dotenv import load_dotenv
        from utils.app_paths import get_app_dir

        # 快照旧值（getattr 带默认值：_ENV_MAP 新增但漏加类属性时不再整段崩溃）
        old = {k: getattr(cls, k, None) for k in _ENV_MAP}

        # 重载 .env 文件
        env_path = os.path.join(get_app_dir(), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(override=True)

        # 从 _ENV_MAP 统一重载，新增配置只需在 _ENV_MAP 添加一行
        for attr, (env, default, conv) in _ENV_MAP.items():
            setattr(cls, attr, conv(os.getenv(env, default)))

        # 返回变更的 key 集合
        return {k for k in _ENV_MAP if getattr(cls, k) != old.get(k)}

    @classmethod
    def validate(cls):
        if not cls.OPENAI_API_KEY:
            raise ValueError("请在 .env 中配置 OPENAI_API_KEY")

        import json
        for field_name in ('OPENAI_HEADERS', 'OPENAI_DEFAULT_PARAMS', 'EXTRA_BODY'):
            raw = getattr(cls, field_name, '')
            if raw:
                try:
                    json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{field_name} JSON 格式错误: {e}")

        # A8 语义缓存 / 路由映射表必须是合法 JSON
        for field_name in ('LLM_CACHE_TTL_MAP', 'LLM_CACHE_CATEGORY_MAP', 'LLM_ROUTING_MAP'):
            raw = getattr(cls, field_name, '')
            if raw:
                try:
                    json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{field_name} JSON 格式错误: {e}")

        if cls.OPENAI_REASONING_CONTENT_POLICY not in ("auto", "always", "never"):
            raise ValueError(
                "OPENAI_REASONING_CONTENT_POLICY 必须是 auto / always / never"
            )

        if cls.OPENAI_REASONING_EFFORT not in ("", "low", "medium", "high", "none"):
            raise ValueError(
                "OPENAI_REASONING_EFFORT 必须是 low / medium / high / none / 留空"
            )

        _INT_FIELDS = (
            'BATCH_SIZE', 'PLAN_MAX_STEPS', 'PLAN_EXECUTOR_MAX_RETRIES',
            'PLAN_EXECUTOR_TOOL_CALLS', 'REACT_TOOL_CALLS', 'REACT_SKILL_BATCH_SIZE', 'REACT_SKILL_RESELECT_EVERY',
            'GROUP_MAX_CONCURRENT_AGENTS', 'GROUP_AGENT_MAX_RETRIES',
            'GROUP_MAX_REPLAN_COUNT', 'GROUP_MAX_STEPS', 'GROUP_MAX_LLM_CALLS',
            'REACT_CONTEXT_RECENT_ROUNDS', 'REACT_SUMMARY_TRIGGER_ROUNDS',
            'REACT_SUMMARY_PENDING_CHARS', 'REACT_SUMMARY_MAX_CHARS',
            'MAX_TOKENS_PER_QUERY', 'MAX_LLM_CALLS_PER_QUERY',
            'MAX_TIME_SECONDS', 'MEMORY_MAX_TOKENS', 'DATASOURCE_MAX_FAILS',
            'CLARIFY_MAX_QUESTIONS', 'CLARIFY_TIMEOUT_SECONDS',
            'CACHE_EXPIRE_HOURS',
            'REPORT_MIN_TOKENS', 'REPORT_MAX_TOOL_OUTPUT_CHARS', 'REPORT_MAX_NEWS_CHARS',
            'REPORT_TECH_PANEL_MAX',
            'REPORT_DRILLDOWN_MIN_TOOLS', 'REPORT_DRILLDOWN_MIN_CHARS',
            'TOOL_OUTPUT_COMPRESS_THRESHOLD', 'TOOL_HARD_TRUNCATE_CHARS', 'INFO_ACCUMULATOR_COMPRESS_THRESHOLD',
            'DASHBOARD_MAX_LLM_TOKENS',
            'MEMORY_RETRIEVAL_TOP_K', 'MEMORY_MAX_PROMPT_TOKENS',
            'MEMORY_EPISODIC_RETENTION_DAYS',
        )
        for field_name in _INT_FIELDS:
            val = getattr(cls, field_name, None)
            if val is not None and not isinstance(val, int):
                raise ValueError(f"{field_name} 必须是整数，当前值: {val}")

        if cls.REACT_DEDUP_MODE not in ("exact", "off"):
            raise ValueError("REACT_DEDUP_MODE 必须是 exact / off")

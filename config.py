"""
选股雷达 - 统一配置管理
"""
import os
from dotenv import load_dotenv
from utils.app_paths import get_db_path, get_logs_dir, get_trace_db_path

load_dotenv()


class Config:
    """统一配置，所有环境变量在这里读取"""

    # LLM
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "gpt-4o")

    # LLM 高级配置
    OPENAI_HEADERS = os.getenv("OPENAI_HEADERS", "")
    OPENAI_DEFAULT_PARAMS = os.getenv("OPENAI_DEFAULT_PARAMS", "")

    # Plan & Solve 模式配置
    PLAN_MAX_STEPS = int(os.getenv("PLAN_MAX_STEPS", "5"))
    PLAN_EXECUTOR_MAX_RETRIES = int(os.getenv("PLAN_EXECUTOR_MAX_RETRIES", "3"))
    PLAN_EXECUTOR_TOOL_CALLS = int(os.getenv("PLAN_EXECUTOR_TOOL_CALLS", "15"))

    # ReAct 模式配置（LangGraph recursion_limit：每个 LLM 调用 + 每个工具调用各算 1 步）
    REACT_TOOL_CALLS = int(os.getenv("REACT_TOOL_CALLS", "25"))

    # 通用Agent配置
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
    MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
    MEMORY_MAX_TOKENS = int(os.getenv("MEMORY_MAX_TOKENS", "8000"))

    # 预算控制
    MAX_TOKENS_PER_QUERY = int(os.getenv("MAX_TOKENS_PER_QUERY", "50000"))
    MAX_LLM_CALLS_PER_QUERY = int(os.getenv("MAX_LLM_CALLS_PER_QUERY", "30"))
    MAX_TIME_SECONDS = int(os.getenv("MAX_TIME_SECONDS", "600"))

    # 缓存（小时）
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

    # 数据库路径 - 动态获取，支持打包部署
    @classmethod
    def get_db_path(cls) -> str:
        return get_db_path()

    @classmethod
    def get_log_dir(cls) -> str:
        return get_logs_dir()

    @property
    def DB_PATH(self) -> str:
        return Config.get_db_path()

    @property
    def LOG_DIR(self) -> str:
        return Config.get_log_dir()

    # 数据源
    DATASOURCE_MAX_FAILS = int(os.getenv("DATASOURCE_MAX_FAILS", "5"))

    # ── 动态配置更新（委托给 ConfigManager）──

    @classmethod
    def set_log_level(cls, level: str):
        from utils.config_manager import ConfigManager
        ConfigManager().set_log_level(level)
        cls.LOG_LEVEL = level.upper()

    @classmethod
    def set_debug_step_confirm(cls, enabled: bool):
        from utils.config_manager import ConfigManager
        ConfigManager().set_debug_step_confirm(enabled)
        cls.DEBUG_STEP_CONFIRM = enabled

    @classmethod
    def validate(cls):
        if not cls.OPENAI_API_KEY:
            raise ValueError("请在 .env 中配置 OPENAI_API_KEY")

        import json
        for field_name in ('OPENAI_HEADERS', 'OPENAI_DEFAULT_PARAMS'):
            raw = getattr(cls, field_name, '')
            if raw:
                try:
                    json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{field_name} JSON 格式错误: {e}")

        _INT_FIELDS = (
            'BATCH_SIZE', 'PLAN_MAX_STEPS', 'PLAN_EXECUTOR_MAX_RETRIES',
            'PLAN_EXECUTOR_TOOL_CALLS', 'REACT_TOOL_CALLS',
            'MAX_TOKENS_PER_QUERY', 'MAX_LLM_CALLS_PER_QUERY',
            'MAX_TIME_SECONDS', 'MEMORY_MAX_TOKENS', 'DATASOURCE_MAX_FAILS',
        )
        for field_name in _INT_FIELDS:
            val = getattr(cls, field_name, None)
            if val is not None and not isinstance(val, int):
                raise ValueError(f"{field_name} 必须是整数，当前值: {val}")
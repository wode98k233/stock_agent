"""
选股雷达 - 统一配置管理（config/ 包）

2026-10-17 由根目录 config.py 按域拆分而来：Config.XXX 访问方式不变，
根目录 config.py 只是兼容壳。新增配置：config/_env_map.py 加一行 +
对应域文件加类属性（双写红线，一致性由 test/unit/test_config_consistency.py 把关）。
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
_env_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
load_dotenv(_env_dir / '.env')

from config._env_map import _ENV_MAP  # noqa: E402
from config.llm import _LLMMixin  # noqa: E402
from config.side_llm import _SideLLMMixin  # noqa: E402
from config.budget import _BudgetMixin  # noqa: E402
from config.agent import _AgentMixin  # noqa: E402
from config.context import _ContextMixin  # noqa: E402
from config.backtest import _BacktestMixin  # noqa: E402
from config.memory import _MemoryMixin  # noqa: E402
from config.datasource import _DatasourceMixin  # noqa: E402
from config.report import _ReportMixin  # noqa: E402
from config.web import _WebMixin  # noqa: E402
from config.scan import _ScanMixin  # noqa: E402
from config.infra import _InfraMixin  # noqa: E402

__all__ = ["Config", "_ENV_MAP"]


class Config(_LLMMixin,
             _SideLLMMixin,
             _BudgetMixin,
             _AgentMixin,
             _ContextMixin,
             _BacktestMixin,
             _MemoryMixin,
             _DatasourceMixin,
             _ReportMixin,
             _WebMixin,
             _ScanMixin,
             _InfraMixin):
    """统一配置，所有环境变量在这里读取"""

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

        # TOOL_SURFACE_MODE / SKILL_ACTIVATE_TOOL_MODE 已固化为常量（2026-10-17
        # 删档，见 config/agent.py），运行时不可再配，无需校验。

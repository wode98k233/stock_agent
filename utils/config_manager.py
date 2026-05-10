"""
选股雷达 - 配置管理器
统一管理所有配置，支持动态更新、验证和变更通知

核心设计：
  1. 配置分组：SystemConfig / AgentConfig / CacheConfig / DataSourceConfig
  2. 动态更新：set_log_level() / set_debug_step_confirm() 等 setter 方法
  3. 配置验证：validate() 统一校验，setter 方法实时校验
  4. 变更通知：add_change_listener() 监听配置变更
  5. 快照恢复：snapshot() / restore() 支持测试隔离
"""
import os
import logging
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field, asdict
from threading import Lock
from dotenv import load_dotenv

load_dotenv()


VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass
class SystemConfig:
    log_level: str = "DEBUG"
    debug_step_confirm: bool = True
    enable_trace: bool = False
    trace_db_path: str = ""


@dataclass
class AgentConfig:
    plan_max_steps: int = 5
    plan_executor_max_retries: int = 3
    plan_executor_tool_calls: int = 5
    react_tool_calls: int = 12
    batch_size: int = 5


@dataclass
class CacheConfig:
    stock_history_hours: int = 24
    board_hours: int = 24
    news_hours: int = 2
    rating_hours: int = 72
    financial_hours: int = 48


@dataclass
class DataSourceConfig:
    max_fails: int = 5
    recovery_secs: int = 600
    request_timeout: int = 15
    tushare_token: str = ""


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key, str(default))
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


class ConfigManager:
    """配置管理器（线程安全单例）"""

    _instance: Optional["ConfigManager"] = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._change_listeners: List[Callable[[str, Any, Any], None]] = []
        self._init_from_env()

    def _init_from_env(self):
        self.system = SystemConfig(
            log_level=os.getenv("LOG_LEVEL", "DEBUG").upper(),
            debug_step_confirm=os.getenv("DEBUG_STEP_CONFIRM", "true").lower() == "true",
            enable_trace=os.getenv("ENABLE_TRACE", "false").lower() == "true",
            trace_db_path=os.getenv("TRACE_DB_PATH", ""),
        )
        self.agent = AgentConfig(
            plan_max_steps=_env_int("PLAN_MAX_STEPS", 5),
            plan_executor_max_retries=_env_int("PLAN_EXECUTOR_MAX_RETRIES", 3),
            plan_executor_tool_calls=_env_int("PLAN_EXECUTOR_TOOL_CALLS", 5),
            react_tool_calls=_env_int("REACT_TOOL_CALLS", 12),
            batch_size=_env_int("BATCH_SIZE", 5),
        )
        self.cache = CacheConfig(
            stock_history_hours=_env_int("CACHE_STOCK_HISTORY_HOURS", 24),
            board_hours=_env_int("CACHE_BOARD_HOURS", 24),
            news_hours=_env_int("CACHE_NEWS_HOURS", 2),
            rating_hours=_env_int("CACHE_RATING_HOURS", 72),
            financial_hours=_env_int("CACHE_FINANCIAL_HOURS", 48),
        )
        self.datasource = DataSourceConfig(
            max_fails=_env_int("DATASOURCE_MAX_FAILS", 5),
            recovery_secs=_env_int("DATASOURCE_RECOVERY_SECS", 600),
            request_timeout=_env_int("REQUEST_TIMEOUT", 15),
            tushare_token=os.getenv("TUSHARE_TOKEN", ""),
        )

    # ── 动态更新（带验证 + 变更通知）──

    def set_log_level(self, level: str) -> None:
        level = level.upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError(f"无效的日志级别: {level}，可选: {VALID_LOG_LEVELS}")
        old = self.system.log_level
        self.system.log_level = level
        self._notify("system.log_level", old, level)

    def set_debug_step_confirm(self, enabled: bool) -> None:
        old = self.system.debug_step_confirm
        self.system.debug_step_confirm = enabled
        self._notify("system.debug_step_confirm", old, enabled)

    def set_enable_trace(self, enabled: bool) -> None:
        old = self.system.enable_trace
        self.system.enable_trace = enabled
        self._notify("system.enable_trace", old, enabled)

    def set_trace_db_path(self, path: str) -> None:
        old = self.system.trace_db_path
        self.system.trace_db_path = path
        self._notify("system.trace_db_path", old, path)

    # ── 配置校验 ──

    def validate(self) -> None:
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("请在 .env 中配置 OPENAI_API_KEY")
        if self.system.log_level not in VALID_LOG_LEVELS:
            raise ValueError(f"无效的日志级别: {self.system.log_level}")
        self._validate_positive_int(self.agent.plan_max_steps, "PLAN_MAX_STEPS")
        self._validate_positive_int(self.agent.plan_executor_max_retries, "PLAN_EXECUTOR_MAX_RETRIES")
        self._validate_positive_int(self.agent.plan_executor_tool_calls, "PLAN_EXECUTOR_TOOL_CALLS")
        self._validate_positive_int(self.agent.react_tool_calls, "REACT_TOOL_CALLS")

    @staticmethod
    def _validate_positive_int(value: int, name: str) -> None:
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} 必须是正整数，当前值: {value}")

    # ── 快照与恢复（测试隔离）──

    def snapshot(self) -> Dict[str, Any]:
        return asdict(self.system)

    def restore(self, snap: Dict[str, Any]) -> None:
        for k, v in snap.items():
            if hasattr(self.system, k):
                setattr(self.system, k, v)

    # ── 变更监听 ──

    def add_change_listener(self, listener: Callable[[str, Any, Any], None]) -> None:
        self._change_listeners.append(listener)

    def remove_change_listener(self, listener: Callable[[str, Any, Any], None]) -> None:
        if listener in self._change_listeners:
            self._change_listeners.remove(listener)

    def _notify(self, key: str, old: Any, new: Any) -> None:
        for listener in self._change_listeners:
            try:
                listener(key, old, new)
            except Exception:
                pass

    # ── 实例重置（仅测试用）──

    def _reset(self) -> None:
        self._initialized = False
        self._change_listeners.clear()


def create_config_manager() -> ConfigManager:
    """创建独立的配置管理器实例（用于测试隔离）"""
    ConfigManager._instance = None
    return ConfigManager()

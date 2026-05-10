"""
预算控制器 - 防止 Token 和调用次数失控
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from threading import Lock


@dataclass
class BudgetLimits:
    max_tokens_per_query: int = 50000
    max_llm_calls_per_query: int = 20
    max_time_seconds: int = 60


class BudgetExceeded(Exception):
    """预算超限异常"""
    def __init__(self, reason: str, current: int, limit: int):
        self.reason = reason
        self.current = current
        self.limit = limit
        super().__init__(f"Budget exceeded: {reason} ({current}/{limit})")


class BudgetController:
    """
    预算控制器，在 executor 和 replanner 中定期检查，超预算就强制输出当前已有的结果。

    使用方法：
    budget = BudgetController()
    budget.check()  # 在每个关键节点调用
    """

    def __init__(self, limits: Optional[BudgetLimits] = None):
        self.limits = limits or BudgetLimits()
        self._tokens_used = 0
        self._llm_calls = 0
        self._start_time = time.time()
        self._enabled = True

    def reset(self):
        """重置计数器（每个新查询开始时调用）"""
        self._tokens_used = 0
        self._llm_calls = 0
        self._start_time = time.time()

    def disable(self):
        """禁用预算检查（用于测试）"""
        self._enabled = False

    def enable(self):
        """启用预算检查"""
        self._enabled = True

    def add_tokens(self, count: int):
        """累计 token 消耗"""
        self._tokens_used += count

    def add_call(self):
        """累计一次 LLM 调用"""
        self._llm_calls += 1

    def get_tokens(self) -> int:
        return self._tokens_used

    def get_calls(self) -> int:
        return self._llm_calls

    def get_elapsed(self) -> float:
        return time.time() - self._start_time

    def check(self, logger: Optional[logging.Logger] = None) -> bool:
        """
        检查预算是否超限。

        返回 True 表示正常，False 表示已强制结束。
        如果超限，抛出 BudgetExceeded 异常。
        """
        if not self._enabled:
            return True

        if self._tokens_used > self.limits.max_tokens_per_query:
            msg = f"Token 预算超限 ({self._tokens_used}/{self.limits.max_tokens_per_query})"
            if logger:
                logger.warning("B", msg)
            raise BudgetExceeded("tokens", self._tokens_used, self.limits.max_tokens_per_query)

        if self._llm_calls > self.limits.max_llm_calls_per_query:
            msg = f"LLM 调用次数超限 ({self._llm_calls}/{self.limits.max_llm_calls_per_query})"
            if logger:
                logger.warning("B", msg)
            raise BudgetExceeded("calls", self._llm_calls, self.limits.max_llm_calls_per_query)

        elapsed = self.get_elapsed()
        if elapsed > self.limits.max_time_seconds:
            msg = f"执行时间超限 ({elapsed:.1f}s/{self.limits.max_time_seconds}s)"
            if logger:
                logger.warning("B", msg)
            raise BudgetExceeded("time", int(elapsed), self.limits.max_time_seconds)

        return True

    def get_status(self) -> dict:
        """获取当前预算状态"""
        return {
            "tokens": self._tokens_used,
            "tokens_limit": self.limits.max_tokens_per_query,
            "tokens_percent": min(100, int(self._tokens_used / self.limits.max_tokens_per_query * 100)),
            "calls": self._llm_calls,
            "calls_limit": self.limits.max_llm_calls_per_query,
            "calls_percent": min(100, int(self._llm_calls / self.limits.max_llm_calls_per_query * 100)),
            "elapsed_seconds": round(self.get_elapsed(), 1),
            "time_limit": self.limits.max_time_seconds,
        }

    def __repr__(self) -> str:
        s = self.get_status()
        return (f"Budget(tokens={s['tokens']}/{s['tokens_limit']} ({s['tokens_percent']}%), "
                f"calls={s['calls']}/{s['calls_limit']} ({s['calls_percent']}%), "
                f"time={s['elapsed_seconds']}s/{s['time_limit']}s)")


class BudgetControllerFactory:
    """预算控制器工厂类，负责创建和管理 BudgetController 实例"""
    
    _default_instance: Optional[BudgetController] = None
    _default_lock = Lock()
    
    @classmethod
    def create(cls, limits: Optional[BudgetLimits] = None) -> BudgetController:
        """创建新的预算控制器实例"""
        if limits is None:
            from config import Config
            limits = BudgetLimits(
                max_tokens_per_query=getattr(Config, 'MAX_TOKENS_PER_QUERY', 50000),
                max_llm_calls_per_query=getattr(Config, 'MAX_LLM_CALLS_PER_QUERY', 20),
                max_time_seconds=getattr(Config, 'MAX_TIME_SECONDS', 60),
            )
        return BudgetController(limits)
    
    @classmethod
    def get_default(cls) -> BudgetController:
        """获取默认的预算控制器实例（单例模式，向后兼容）"""
        if cls._default_instance is None:
            with cls._default_lock:
                if cls._default_instance is None:
                    cls._default_instance = cls.create()
        return cls._default_instance


def get_budget_controller() -> BudgetController:
    """获取全局预算控制器实例（向后兼容）"""
    return BudgetControllerFactory.get_default()

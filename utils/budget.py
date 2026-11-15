"""预算控制器 - 防止 Token 和调用次数失控
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from threading import Lock


@dataclass
class BudgetLimits:
    # 计费口径上限（命中/免费已折算）
    max_tokens_per_query: int = 50000
    max_llm_calls_per_query: int = 20
    max_time_seconds: int = 60
    # 原始 token 硬上限（0 = 不启用）。兜底用：权重配错时它先炸，
    # 避免"加权后看起来没超、实际已经烧了很多"。
    max_raw_tokens_per_query: int = 0


class BudgetExceeded(Exception):
    """预算超限异常"""
    def __init__(self, reason: str, current: int, limit: int):
        self.reason = reason
        self.current = current
        self.limit = limit
        super().__init__(f"Budget exceeded: {reason} ({current}/{limit})")


class BudgetExemptWindow:
    """预算豁免窗口 — 用户确认超限后，短期内跳过 budget check"""

    def __init__(self, calls_limit=3, time_limit=60):
        self.remaining_calls = calls_limit
        self.start_time = time.time()
        self.time_limit = time_limit

    def is_active(self) -> bool:
        if self.remaining_calls <= 0:
            return False
        if time.time() - self.start_time > self.time_limit:
            return False
        return True

    def consume(self):
        self.remaining_calls -= 1

    def to_dict(self) -> dict:
        return {
            "remaining_calls": self.remaining_calls,
            "start_time": self.start_time,
            "time_limit": self.time_limit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetExemptWindow":
        w = cls(calls_limit=d["remaining_calls"], time_limit=d["time_limit"])
        w.start_time = d["start_time"]
        return w


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
        # 计费口径 token（PRD-A11 FR-6）：命中按 in_hit/in_miss 折算、免费端点按
        # free 权重折算。`_tokens_used` 仍是**原始**口径，两个并存。
        self._weighted_used = 0.0
        self._llm_calls = 0
        self._start_time = time.time()
        self._excluded_time = 0.0  # 扣除的等待耗时（澄清反问等用户交互阻塞）
        self._enabled = True
        self.exempt_window: Optional[BudgetExemptWindow] = None

    def reset(self):
        """重置计数器（每个新查询开始时调用）"""
        self._tokens_used = 0
        self._weighted_used = 0.0
        self._llm_calls = 0
        self._start_time = time.time()
        self._excluded_time = 0.0
        self.exempt_window = None

    def exclude_seconds(self, seconds: float) -> None:
        """从 elapsed 里扣掉一段等待时间。

        用于「等用户作答」这类不消耗算力的阻塞（澄清反问最长 5 分钟），
        否则会白白吃掉 MAX_TIME_SECONDS 时间预算。
        """
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            return
        if sec > 0:
            self._excluded_time += sec

    def disable(self):
        """禁用预算检查（用于测试）"""
        self._enabled = False

    def enable(self):
        """启用预算检查"""
        self._enabled = True

    def add_tokens(self, count: int, *, weighted: float | None = None):
        """累计 token 消耗。

        :param count: **原始** token（input+output），永远累加 —— 这是
            `MAX_RAW_TOKENS_PER_QUERY` 的判据，也是"真实消耗"的事实源。
        :param weighted: 计费口径 token（PRD-A11 FR-6）。None 时按原始计
            （向后兼容：老调用方只传 count 时行为与改动前完全一致）。

        **为什么需要加权**：prompt cache 命中价是未命中的 1/50，免费端点更是 ¥0，
        但两者过去都按原始 token 100% 计入预算 —— 等于"缓存优化被预算惩罚"，
        且免费调用白占额度（实测占 raw 的 26%）。
        """
        self._tokens_used += count
        self._weighted_used += float(count if weighted is None else weighted)

    def add_call(self):
        """累计一次 LLM 调用"""
        self._llm_calls += 1

    def get_tokens(self) -> int:
        """原始 token（含命中与免费端点）—— 与改动前语义一致。"""
        return self._tokens_used

    def get_weighted_tokens(self) -> float:
        """计费口径 token —— 预算判定用这个。"""
        return self._weighted_used

    def get_calls(self) -> int:
        return self._llm_calls

    def get_elapsed(self) -> float:
        return max(0.0, time.time() - self._start_time - getattr(self, "_excluded_time", 0.0))

    def set_exempt_window(self, window: BudgetExemptWindow):
        """设置预算豁免窗口"""
        self.exempt_window = window

    def clear_exempt_window(self):
        """清除预算豁免窗口"""
        self.exempt_window = None

    def check(self, logger: Optional[logging.Logger] = None) -> bool:
        """
        检查预算是否超限。

        返回 True 表示正常，False 表示已强制结束。
        如果超限，抛出 BudgetExceeded 异常。
        """
        if not self._enabled:
            return True

        # 豁免窗口检查
        if self.exempt_window and self.exempt_window.is_active():
            self.exempt_window.consume()
            if logger:
                logger.info("B", f"豁免窗口内，跳过 budget check（剩余 {self.exempt_window.remaining_calls} 次）")
            return True

        # ① 原始 token 硬兜底：权重配错时唯一能拦住失控的东西（PRD-A11 FR-6）
        raw_limit = int(getattr(self.limits, "max_raw_tokens_per_query", 0) or 0)
        if raw_limit and self._tokens_used > raw_limit:
            msg = (f"原始 Token 硬上限 ({self._tokens_used}/{raw_limit})"
                   f"（计费口径 {self._weighted_used:.0f}/{self.limits.max_tokens_per_query}）")
            if logger:
                logger.warning("B", msg)
            raise BudgetExceeded("raw_tokens", self._tokens_used, raw_limit)

        # ② 计费口径判定（命中与免费端点已折算）
        if self._weighted_used > self.limits.max_tokens_per_query:
            msg = (f"Token 预算超限（计费口径 {self._weighted_used:.0f}/"
                   f"{self.limits.max_tokens_per_query}，原始 {self._tokens_used}）")
            if logger:
                logger.warning("B", msg)
            raise BudgetExceeded("tokens", int(self._weighted_used),
                                 self.limits.max_tokens_per_query)

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
        limit = self.limits.max_tokens_per_query or 1
        raw_limit = int(getattr(self.limits, "max_raw_tokens_per_query", 0) or 0)
        return {
            # 兼容：tokens 收紧为**计费口径**（PRD-A11 FR-6 起语义变化）；
            # 需要原始总量看 tokens_raw，两者同时暴露，看板与日志都能取。
            "tokens": int(self._weighted_used),
            "tokens_raw": self._tokens_used,
            "tokens_weighted": int(self._weighted_used),
            "tokens_limit": self.limits.max_tokens_per_query,
            "tokens_raw_limit": raw_limit,
            "tokens_raw_percent": (min(100, int(self._tokens_used / raw_limit * 100))
                                   if raw_limit else None),
            "tokens_percent": min(100, int(self._weighted_used / limit * 100)),
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
                max_raw_tokens_per_query=getattr(Config, 'MAX_RAW_TOKENS_PER_QUERY', 0),
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

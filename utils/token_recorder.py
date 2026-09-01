"""统一 Token 记录器 — 三位一体架构

设计目标：
- metrics、budget、trace 从同一事件获取相同 token 数
- 可插拔后端：新增记录目标只需实现 TokenBackend
- 业务层屏蔽：tracked_invoke 只需传入 TokenRecorder，无需知道后端细节

Trace 通过 register_configure_hook 全局注入，独立于此模块。
"""
from __future__ import annotations

import time
from typing import Protocol

from langchain_core.callbacks.base import BaseCallbackHandler

from utils.budget import BudgetExceeded
from utils.logger import ensure_radar


class TokenBackend(Protocol):
    """Token 记录后端接口"""
    def record(self, tokens_in: int, tokens_out: int, label: str, cached_tokens: int = 0,
               reasoning_tokens: int = 0) -> None: ...


class MetricsBackend(TokenBackend):
    """记录到 RequestContext（metrics 汇总）"""
    def record(self, tokens_in: int, tokens_out: int, label: str, cached_tokens: int = 0,
               reasoning_tokens: int = 0) -> None:
        try:
            from utils.logger import RequestContext
            ctx = RequestContext.current()
            if ctx:
                ctx.record_llm(tokens_in, tokens_out, cached_tokens, reasoning_tokens)
        except Exception:
            pass


class BudgetBackend(TokenBackend):
    """记录到 BudgetController（预算控制）"""
    def __init__(self, budget):
        self.budget = budget

    def record(self, tokens_in: int, tokens_out: int, label: str, cached_tokens: int = 0,
               reasoning_tokens: int = 0) -> None:
        # 思考 token 计入总量（思考也是真实消耗，预算不豁免）
        if self.budget:
            self.budget.add_tokens(tokens_in + tokens_out)
            self.budget.add_call()
            self.budget.check(ensure_radar(None))


class TokenRecorder(BaseCallbackHandler):
    """统一 Token 记录器，可插拔后端

    用法：
        backends = [MetricsBackend()]
        if budget:
            backends.append(BudgetBackend(budget))
        recorder = TokenRecorder(logger, "label", backends)
    """

    def __init__(self, logger, label: str = "", backends: list[TokenBackend] = None, budget=None):
        self.logger = ensure_radar(logger)
        self.label = label
        # 向后兼容：如果传了 budget 但没传 backends，自动构建
        if backends is not None:
            self.backends = backends
        else:
            self.backends = [MetricsBackend()]
            if budget:
                self.backends.append(BudgetBackend(budget))
        self.t0 = None
        self.budget_exceeded = False

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.t0 = time.time()

    def on_llm_end(self, response, **kwargs):
        dt = time.time() - (self.t0 or time.time())
        from utils.token_usage import extract_token_usage
        usage = extract_token_usage(response)
        inp = usage["input_tokens"]
        out = usage["output_tokens"]

        cached = usage.get("cached_tokens", 0)
        reasoning = usage.get("reasoning_tokens", 0)
        for backend in self.backends:
            try:
                backend.record(inp, out, self.label, cached, reasoning)
            except BudgetExceeded:
                self.budget_exceeded = True
            except Exception:
                pass

        self.logger.llm_call(self.label, dt, inp, out,
                             cached_tokens=usage.get("cached_tokens", 0),
                             cache_hit_ratio=usage.get("cache_hit_ratio", 0.0),
                             reasoning_tokens=reasoning)

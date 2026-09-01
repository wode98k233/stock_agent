"""
选股雷达 — LLM 网关门面
======================

**这是整个网关的「总装车间」**。它把前面各模块串成一条策略链，对「所有」LLM 调用
（含 chat / embed / rerank）统一施加：

    请求 → router(选主, 失败切 fallback) → ratelimit(令牌桶, 调用前节流)
         → [实际调用] → retry(复用内核 _is_retryable_error + 退避)
         → breaker(连续失败开闸, 快速失败) → meter(记延迟/成本)

设计要点：
- **chat**：``chat()`` 返回「已套策略链」的模型（``GatewayChatModel``），可直接 ``.invoke/.ainvoke/.stream/.astream``，
  也可交给现有 ``tracked_invoke`` 使用（不会重复记 token）。``invoke/ainvoke`` 一步到位。
- **stream**：``.stream/.astream`` 同样走策略链 —— 限流/熔断/router 选主在「首 chunk 前」生效；
  首 chunk 之后中途失败不切模型（流式语义无法中途换模型）。底层 ``config``（callbacks/run_id）透传，
  token 埋点复用链上 recorder，与 ``invoke`` 行为一致。
- **embed / rerank 升为一等公民**：同样走策略链（限流/重试/熔断/追踪），不再是旁路。
- **降级**：主模型持续失败（熔断开闸或重试耗尽）→ 按 registry 的 fallbacks 切下一个模型。
- **可扩展**：加用途/加 provider/加策略都不用改这里的编排（见各子模块注释）。

==================================================================
【用法速查】
==================================================================
    from utils.llm import gateway
    llm = gateway.chat("agent")                 # 已套策略链，可 .invoke/.ainvoke/.stream/.astream
    gateway.invoke("agent", messages, logger, label="agent")   # 一步到位（同步）
    await gateway.ainvoke("report", messages, logger, label="report")  # 异步
    for chunk in gateway.stream("agent", messages): ...        # 流式（已限流/熔断/降级）
    vec = gateway.embed([text], purpose="memory")[0]            # embedding
    scores = gateway.rerank(query, docs, purpose="memory")      # rerank

【扩展指引】（详见 docs/superpowers/specs/2026-07-12-llm-gateway-design.md §7）
- 加 chat 用途：types.Purpose 加成员 + registry 配模型，调用点零改动。
- 加 provider：backends/ 加分支。
- 加策略：policies/ 加模块，在这里策略链里插一行。
- 换独立代理（LiteLLM proxy）：改 backends/chat.build_chat_model 的 base_url 即可，
  调用点（gateway.chat）一行不改。
"""

import logging
import os
from dataclasses import replace
from typing import List, Optional

from utils.llm.types import Purpose, ModelProfile, ChatModelSpec, RateLimitConfig
from utils.llm.registry import ModelRegistry
from utils.llm.policies.ratelimit import TokenBucket, RateLimited
from utils.llm.policies.router import build_chain
from utils.llm.policies.retry import retry_call, aretry_call
from utils.llm.policies.breaker import CircuitBreaker, CircuitOpenError
from utils.llm.backends.chat import build_chat_model
from utils.llm.backends.embed import EmbeddingProvider
from utils.llm.backends.rerank import RerankProvider
from utils.llm_factory import (
    tracked_invoke,
    atracked_invoke,
    _build_llm_config,
    _is_retryable_error,
    _find_token_recorder,
    TokenCompatibleChatOpenAI,
)

_log = logging.getLogger("utils.llm.gateway")


def _est_tokens(messages) -> int:
    """粗略估算输入 token 数（用于 tpm 限流）。用 len//4 兜底，避免引入 tokenizer 依赖。"""
    total = 0
    for m in messages:
        content = getattr(m, "content", None)
        if content is None:
            content = str(m)
        elif not isinstance(content, str):
            content = str(content)
        total += max(1, len(content) // 4)
    return total or 1


class GatewayChatModel(TokenCompatibleChatOpenAI):
    """一个「已套策略链」的 chat 模型句柄。

    它本身是合法的 BaseChatModel（``tracked_invoke`` 可直接使用），但 ``.invoke/.ainvoke``
    会走网关的策略链：按 fallback 顺序尝试各模型，套限流/重试/熔断/计量。
    """

    def __init__(self, gateway: "LLMGateway", purpose: Purpose, spec: ChatModelSpec,
                 fallback: bool = True, overrides: Optional[dict] = None):
        # 构造底层 primary 模型（未配置时给占位，避免构造期崩溃；真正的错误在 invoke 时才暴露）
        p = spec.primary
        try:
            super().__init__(
                model=p.model or "unconfigured",
                api_key=(p.api_key or "unconfigured") or None,
                base_url=p.base_url or None,
                temperature=p.temperature,
                model_kwargs={},
                reasoning_content_policy=p.reasoning_content_policy,
                extra_body={},
                headers=p.headers,
                default_params=p.default_params,
            )
        except Exception:
            # 极罕见：构造失败也不崩网关，invoke 时会拿到明确错误
            pass
        # 用 object.__setattr__ 绕过 pydantic v2 的 __setattr__，把私有属性落进实例 __dict__。
        # 否则在 super().__init__() 之前/之中赋值会被 pydantic 丢弃，
        # 导致后续 invoke/stream/ainvoke 访问 self._gateway 报 AttributeError。
        object.__setattr__(self, "_gateway", gateway)
        object.__setattr__(self, "_purpose", purpose)
        object.__setattr__(self, "_spec", spec)
        object.__setattr__(self, "_fallback", fallback)
        object.__setattr__(self, "_overrides", overrides or {})

    def invoke(self, messages, config=None, *args, **kwargs):
        # config 为 LangChain RunnableConfig（链内部按位置传入，含 callbacks/run_id/tags）。
        # 必须作为具名参数接住并透传 —— 否则嵌套进 LangGraph 时回调链 / run_id 传播会断裂，
        # token 记录器也取不到链上的 logger/label。详见 ADR-004「已知限制」修复记录。
        logger = self._gateway._logger_from_config(config) or _log
        label = self._gateway._label_from_config(config) or self._purpose.value
        return self._gateway._run_chat_sync(
            self._spec, self._fallback, messages, logger, label, None, None, None, config, *args, **kwargs
        )

    async def ainvoke(self, messages, config=None, *args, **kwargs):
        logger = self._gateway._logger_from_config(config) or _log
        label = self._gateway._label_from_config(config) or self._purpose.value
        return await self._gateway._run_chat_async(
            self._spec, self._fallback, messages, logger, label, None, None, None, config, *args, **kwargs
        )

    def stream(self, messages, config=None, *args, **kwargs):
        """流式 chat（已套网关策略链）。

        限流 / 熔断 / router 选主在「产出首 chunk 前」生效；首 chunk 之后中途失败不切模型
        （流式语义无法中途换模型）。底层 config（callbacks/run_id）透传，token 埋点复用链上 recorder。
        """
        logger = self._gateway._logger_from_config(config) or _log
        label = self._gateway._label_from_config(config) or self._purpose.value
        return self._gateway._run_chat_stream_sync(
            self._spec, self._fallback, messages, logger, label, None, None, None, config, *args, **kwargs
        )

    async def astream(self, messages, config=None, *args, **kwargs):
        logger = self._gateway._logger_from_config(config) or _log
        label = self._gateway._label_from_config(config) or self._purpose.value
        async for chunk in self._gateway._run_chat_stream_async(
            self._spec, self._fallback, messages, logger, label, None, None, None, config, *args, **kwargs
        ):
            yield chunk


class LLMGateway:
    """LLM 网关门面（单例，见 ``utils/llm.__init__`` 的 ``gateway``）。"""

    def __init__(self, registry: Optional[ModelRegistry] = None,
                 breaker_threshold: Optional[int] = None,
                 breaker_cooldown: Optional[float] = None,
                 chat_retries: Optional[int] = None):
        """
        :param registry: 模型注册表；留空则按 env + 可选 models.yaml 自动构建。
        :param breaker_threshold: 熔断器开闸阈值；为 None 时读 env LLM_GATEWAY_BREAKER_THRESHOLD，
            再退化为默认 5。构造函数参数优先级高于 env，便于测试显式覆盖。
        :param breaker_cooldown: 熔断器冷却秒数；env LLM_GATEWAY_BREAKER_COOLDOWN，默认 30.0。
        :param chat_retries: 主模型瞬时错误重试次数；env LLM_GATEWAY_CHAT_RETRIES，默认 2。
        """
        self._registry = registry or ModelRegistry()
        # 优先级：显式构造参数 > 环境变量 > 内置默认。env 让运维不改代码即可调参。
        _env_bt = os.getenv("LLM_GATEWAY_BREAKER_THRESHOLD")
        _env_bc = os.getenv("LLM_GATEWAY_BREAKER_COOLDOWN")
        _env_cr = os.getenv("LLM_GATEWAY_CHAT_RETRIES")
        # 优先级：显式构造参数 > 环境变量 > 内置默认。env 让运维不改代码即可调参，
        # 显式参数（如测试）始终优先，避免被 env 意外覆盖。
        self._breaker_threshold = breaker_threshold if breaker_threshold is not None else (int(_env_bt) if _env_bt else 5)
        self._breaker_cooldown = breaker_cooldown if breaker_cooldown is not None else (float(_env_bc) if _env_bc else 30.0)
        self._chat_retries = chat_retries if chat_retries is not None else (int(_env_cr) if _env_cr else 2)
        self._limiters = {}     # Purpose -> TokenBucket
        self._breakers = {}     # (purpose, model_key) -> CircuitBreaker
        self._embed_provider = None
        self._rerank_provider = None

    # ── 缓存：限流器 / 熔断器 ──

    def _limiter_for(self, purpose) -> TokenBucket:
        key = Purpose.coerce(purpose)
        if key not in self._limiters:
            if key == Purpose.MEMORY:
                limits: RateLimitConfig = self._registry.get_embed_spec().limits
            else:
                limits = self._registry.get_chat_spec(key).limits
            self._limiters[key] = TokenBucket(rpm=limits.rpm, tpm=limits.tpm)
        return self._limiters[key]

    def _breaker_for(self, purpose, model_key: str) -> CircuitBreaker:
        key = (Purpose.coerce(purpose).value, model_key)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(
                threshold=self._breaker_threshold, cooldown=self._breaker_cooldown
            )
        return self._breakers[key]

    # ── 从 config(callbacks) 解析 logger / label（避免双重 token 记录） ──

    @staticmethod
    def _logger_from_config(config):
        rec = _find_token_recorder(config)
        if rec is not None and hasattr(rec, "logger"):
            return rec.logger
        return None

    @staticmethod
    def _label_from_config(config):
        rec = _find_token_recorder(config)
        if rec is not None and hasattr(rec, "label"):
            return rec.label
        return None

    # ── spec 解析（支持覆盖 primary 字段） ──

    def _resolve_spec(self, purpose, overrides: Optional[dict]) -> ChatModelSpec:
        spec = self._registry.get_chat_spec(purpose)
        if not overrides:
            return spec
        fields = {k: overrides[k] for k in ("model", "api_key", "base_url", "temperature") if k in overrides}
        if not fields:
            return spec
        primary = replace(spec.primary, **fields)
        return replace(spec, primary=primary)

    # ── 公开：chat / achat ──

    def chat(self, purpose, *, fallback: bool = True, **overrides) -> GatewayChatModel:
        """返回一个「已套策略链」的 chat 模型。"""
        spec = self._resolve_spec(purpose, overrides or None)
        return GatewayChatModel(self, Purpose.coerce(purpose), spec, fallback=fallback)

    async def achat(self, purpose, *, fallback: bool = True, **overrides) -> GatewayChatModel:
        return self.chat(purpose, fallback=fallback, **overrides)

    # ── 公开：一步到位的 chat 调用 ──

    def invoke(self, purpose, messages, logger, label: str = "", *,
               budget=None, metadata=None, parent_run_id=None, run_config=None, **kwargs):
        spec = self._resolve_spec(purpose, None)
        return self._run_chat_sync(
            spec, True, messages, logger, label or Purpose.coerce(purpose).value,
            budget, metadata, parent_run_id, run_config, **kwargs
        )

    async def ainvoke(self, purpose, messages, logger, label: str = "", *,
                      budget=None, metadata=None, parent_run_id=None, run_config=None, **kwargs):
        spec = self._resolve_spec(purpose, None)
        return await self._run_chat_async(
            spec, True, messages, logger, label or Purpose.coerce(purpose).value,
            budget, metadata, parent_run_id, run_config, **kwargs
        )

    # ── 公开：流式 chat（已套策略链） ──

    def stream(self, purpose, messages, *, budget=None, metadata=None, parent_run_id=None, config=None, **kwargs):
        """流式 chat，已套限流 / 熔断 / router / fallback（首 chunk 前）。返回 chunk 迭代器。"""
        spec = self._resolve_spec(purpose, None)
        return self._run_chat_stream_sync(
            spec, True, messages, _log, Purpose.coerce(purpose).value,
            budget, metadata, parent_run_id, config, **kwargs
        )

    async def astream(self, purpose, messages, *, budget=None, metadata=None, parent_run_id=None, config=None, **kwargs):
        spec = self._resolve_spec(purpose, None)
        return self._run_chat_stream_async(
            spec, True, messages, _log, Purpose.coerce(purpose).value,
            budget, metadata, parent_run_id, config, **kwargs
        )

    # ── 内部：chat 策略链（同步） ──

    def _run_chat_sync(self, spec, fallback, messages, logger, label,
                       budget, metadata, parent_run_id, config, *args, **kwargs):
        logger = logger or _log
        profiles = build_chain(spec) if fallback else [spec.primary]
        last_exc = None
        est = _est_tokens(messages)
        for profile in profiles:
            limiter = self._limiter_for(spec.purpose)
            breaker = self._breaker_for(spec.purpose, profile.model)
            # 限流是「全局」的（按用途），不够直接抛，不切 fallback
            limiter.acquire(est)
            inner = build_chat_model(profile)
            try:
                result = breaker.call(
                    lambda: tracked_invoke(
                        inner, messages, logger, label,
                        budget=budget, metadata=metadata, parent_run_id=parent_run_id,
                        run_config=config, max_retries=self._chat_retries, *args, **kwargs
                    )
                )
                return result
            except CircuitOpenError:
                # 熔断开闸 → 降级到下一个模型
                last_exc = CircuitOpenError(f"circuit open for {profile.model}")
                _log.warning(f"[%s] 熔断 %s，降级", label, profile.model)
                continue
            except Exception as e:
                if _is_retryable_error(e):
                    # tracked_invoke 已重试耗尽仍失败 → 降级
                    last_exc = e
                    _log.warning(f"[%s] %s 重试耗尽，降级", label, profile.model)
                    continue
                raise  # 非重试错误：直接抛，不降级
        raise last_exc or RuntimeError(f"[{label}] 所有模型均不可用")

    # ── 内部：chat 策略链（异步） ──

    async def _run_chat_async(self, spec, fallback, messages, logger, label,
                              budget, metadata, parent_run_id, config, *args, **kwargs):
        logger = logger or _log
        profiles = build_chain(spec) if fallback else [spec.primary]
        last_exc = None
        est = _est_tokens(messages)
        for profile in profiles:
            limiter = self._limiter_for(spec.purpose)
            breaker = self._breaker_for(spec.purpose, profile.model)
            await limiter.aacquire(est)
            inner = build_chat_model(profile)
            try:
                result = await breaker.acall(
                    lambda: atracked_invoke(
                        inner, messages, logger, label,
                        budget=budget, metadata=metadata, parent_run_id=parent_run_id,
                        run_config=config, max_retries=self._chat_retries, *args, **kwargs
                    )
                )
                return result
            except CircuitOpenError:
                last_exc = CircuitOpenError(f"circuit open for {profile.model}")
                _log.warning(f"[%s] 熔断 %s，降级", label, profile.model)
                continue
            except Exception as e:
                if _is_retryable_error(e):
                    last_exc = e
                    _log.warning(f"[%s] %s 重试耗尽，降级", label, profile.model)
                    continue
                raise
        raise last_exc or RuntimeError(f"[{label}] 所有模型均不可用")

    # ── 内部：chat 流式策略链（同步） ──

    def _run_chat_stream_sync(self, spec, fallback, messages, logger, label,
                              budget, metadata, parent_run_id, config, *args, **kwargs):
        logger = logger or _log
        profiles = build_chain(spec) if fallback else [spec.primary]
        last_exc = None
        est = _est_tokens(messages)

        def _start(inner):
            # 在 breaker 内拉首 chunk：首 chunk 前失败 → 记失败→降级；
            # 首 chunk 之后中途失败不切模型（流式语义无法中途换模型）。
            gen = inner.stream(messages, llm_config)
            try:
                first = next(gen)
            except StopIteration:
                return None, gen
            return first, gen

        for profile in profiles:
            limiter = self._limiter_for(spec.purpose)
            breaker = self._breaker_for(spec.purpose, profile.model)
            limiter.acquire(est)  # 限流是「全局」的（按用途），不够直接抛，不切 fallback
            inner = build_chat_model(profile)
            llm_config = _build_llm_config(logger, label, config, budget, metadata, parent_run_id, **kwargs)
            try:
                first, gen = breaker.call(lambda: _start(inner))
            except CircuitOpenError:
                last_exc = CircuitOpenError(f"circuit open for {profile.model}")
                _log.warning(f"[%s] 熔断 %s，降级", label, profile.model)
                continue
            except Exception as e:
                if _is_retryable_error(e):
                    last_exc = e
                    _log.warning(f"[%s] %s 重试耗尽，降级", label, profile.model)
                    continue
                raise
            if first is not None:
                yield first
            yield from gen
            return
        raise last_exc or RuntimeError(f"[{label}] 所有模型均不可用")

    # ── 内部：chat 流式策略链（异步） ──

    async def _run_chat_stream_async(self, spec, fallback, messages, logger, label,
                                     budget, metadata, parent_run_id, config, *args, **kwargs):
        logger = logger or _log
        profiles = build_chain(spec) if fallback else [spec.primary]
        last_exc = None
        est = _est_tokens(messages)

        async def _start(inner):
            gen = inner.astream(messages, llm_config)
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                return None, gen
            return first, gen

        for profile in profiles:
            limiter = self._limiter_for(spec.purpose)
            breaker = self._breaker_for(spec.purpose, profile.model)
            await limiter.aacquire(est)
            inner = build_chat_model(profile)
            llm_config = _build_llm_config(logger, label, config, budget, metadata, parent_run_id, **kwargs)
            try:
                first, gen = await breaker.acall(lambda: _start(inner))
            except CircuitOpenError:
                last_exc = CircuitOpenError(f"circuit open for {profile.model}")
                _log.warning(f"[%s] 熔断 %s，降级", label, profile.model)
                continue
            except Exception as e:
                if _is_retryable_error(e):
                    last_exc = e
                    _log.warning(f"[%s] %s 重试耗尽，降级", label, profile.model)
                    continue
                raise
            if first is not None:
                yield first
            async for chunk in gen:
                yield chunk
            return
        raise last_exc or RuntimeError(f"[{label}] 所有模型均不可用")

    # ── 公开：embedding（一等公民） ──

    def _get_embed_provider(self) -> EmbeddingProvider:
        if self._embed_provider is None:
            self._embed_provider = EmbeddingProvider(self._registry.get_embed_spec())
        return self._embed_provider

    def embed(self, texts: List[str], purpose: Purpose = Purpose.MEMORY) -> List[List[float]]:
        provider = self._get_embed_provider()
        limiter = self._limiter_for(purpose)
        breaker = self._breaker_for(purpose, "embed:" + provider.spec.provider)
        est = sum(max(1, len(t) // 4) for t in texts) or 1
        limiter.acquire(est)
        return breaker.call(lambda: retry_call(lambda: provider.embed(texts), max_retries=3))

    async def aembed(self, texts: List[str], purpose: Purpose = Purpose.MEMORY) -> List[List[float]]:
        provider = self._get_embed_provider()
        limiter = self._limiter_for(purpose)
        breaker = self._breaker_for(purpose, "embed:" + provider.spec.provider)
        est = sum(max(1, len(t) // 4) for t in texts) or 1
        await limiter.aacquire(est)
        return await breaker.acall(lambda: aretry_call(lambda: provider.embed(texts), max_retries=3))

    # ── 公开：rerank（一等公民） ──

    def _get_rerank_provider(self) -> RerankProvider:
        if self._rerank_provider is None:
            self._rerank_provider = RerankProvider(self._registry.get_rerank_spec())
            self._rerank_provider.warm()  # 后台预热本地模型，不阻塞
        return self._rerank_provider

    def rerank(self, query: str, docs: List[str], purpose: Purpose = Purpose.MEMORY) -> List[float]:
        provider = self._get_rerank_provider()
        limiter = self._limiter_for(purpose)
        breaker = self._breaker_for(purpose, "rerank:" + provider.spec.provider)
        est = max(1, len(query) // 4) + sum(max(1, len(d) // 4) for d in docs)
        limiter.acquire(est)
        return breaker.call(lambda: retry_call(lambda: provider.rerank(query, docs), max_retries=3))

    async def arerank(self, query: str, docs: List[str], purpose: Purpose = Purpose.MEMORY) -> List[float]:
        provider = self._get_rerank_provider()
        limiter = self._limiter_for(purpose)
        breaker = self._breaker_for(purpose, "rerank:" + provider.spec.provider)
        est = max(1, len(query) // 4) + sum(max(1, len(d) // 4) for d in docs)
        await limiter.aacquire(est)
        return await breaker.acall(lambda: aretry_call(lambda: provider.rerank(query, docs), max_retries=3))

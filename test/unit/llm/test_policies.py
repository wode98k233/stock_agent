"""策略层单元测试：ratelimit / router / retry / breaker。"""
import asyncio

import pytest

from utils.llm.policies.ratelimit import TokenBucket, RateLimited
from utils.llm.policies.router import build_chain
from utils.llm.policies.retry import retry_call, aretry_call
from utils.llm.policies.breaker import CircuitBreaker, CircuitOpenError
from utils.llm.types import Purpose, ModelProfile, ChatModelSpec


# ── ratelimit ──

def test_ratelimit_rpm_blocks_when_exhausted():
    b = TokenBucket(rpm=2, tpm=0)
    b.acquire(1, blocking=False)  # ok
    b.acquire(1, blocking=False)  # ok
    with pytest.raises(RateLimited):
        b.acquire(1, blocking=False)  # 第三发触发限流


def test_ratelimit_tpm_blocks_when_exhausted():
    b = TokenBucket(rpm=0, tpm=10)
    b.acquire(5, blocking=False)  # ok (10 -> 5)
    b.acquire(5, blocking=False)  # ok (5 -> 0)
    with pytest.raises(RateLimited):
        b.acquire(5, blocking=False)  # 0 < 5 → 限流


def test_ratelimit_zero_means_unlimited():
    b = TokenBucket(rpm=0, tpm=0)
    for _ in range(100):
        b.acquire(1, blocking=False)  # 不应抛


async def test_ratelimit_async_acquire():
    b = TokenBucket(rpm=1, tpm=0)
    await b.aacquire(1, blocking=False)
    with pytest.raises(RateLimited):
        await b.aacquire(1, blocking=False)


# ── router ──

def test_router_build_chain_order():
    spec = ChatModelSpec(
        purpose=Purpose.AGENT,
        primary=ModelProfile(model="m1"),
        fallbacks=[ModelProfile(model="m2"), ModelProfile(model="m3")],
    )
    chain = build_chain(spec)
    assert [p.model for p in chain] == ["m1", "m2", "m3"]


# ── retry ──

def test_retry_retries_transient_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")  # ConnectionError 被 _is_retryable_error 识别
        return "ok"

    assert retry_call(fn, max_retries=3) == "ok"
    assert calls["n"] == 3


def test_retry_non_retryable_raises_immediately():
    def fn():
        raise ValueError("fatal")

    with pytest.raises(ValueError):
        retry_call(fn, max_retries=3)


async def test_aretry_async():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("transient")
        return "ok"

    assert await aretry_call(fn, max_retries=3) == "ok"
    assert calls["n"] == 2


# ── breaker ──

def test_breaker_opens_after_threshold_and_quick_fails():
    br = CircuitBreaker(threshold=2, cooldown=100.0)

    def always_fail():
        raise ValueError("boom")

    for _ in range(2):
        with pytest.raises(ValueError):
            br.call(always_fail)
    assert br.state == "OPEN"
    # 开闸后快速失败
    with pytest.raises(CircuitOpenError):
        br.call(always_fail)


def test_breaker_half_open_recovers():
    br = CircuitBreaker(threshold=2, cooldown=0.0)

    def always_fail():
        raise ValueError("boom")

    for _ in range(2):
        with pytest.raises(ValueError):
            br.call(always_fail)
    assert br.state == "OPEN"
    # cooldown=0 → 下一次进入 HALF_OPEN，探测成功 → 复位 CLOSED
    assert br.call(lambda: "recovered") == "recovered"
    assert br.state == "CLOSED"

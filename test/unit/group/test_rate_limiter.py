import asyncio
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agents.group.rate_limiter import AgentRateLimiter, RateLimitError


@pytest.fixture
def radar_logger():
    """创建一个真实的 RadarLogger 用于测试"""
    from utils.logger import RadarLogger
    raw = logging.getLogger("test_rate_limiter")
    return RadarLogger(raw)


@pytest.mark.asyncio
async def test_rate_limiter_success(radar_logger):
    limiter = AgentRateLimiter(max_concurrent=2, max_retries=2)

    async def produce_ok():
        return "ok"

    result = await limiter.run_with_retry(
        coro_factory=produce_ok,
        agent_name="test",
        logger=radar_logger,
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_rate_limiter_retries_on_rate_limit(radar_logger):
    limiter = AgentRateLimiter(max_concurrent=2, max_retries=2)
    call_count = 0

    async def sometimes_rate_limit():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RateLimitError("rate limited")
        return "success"

    with patch("agents.group.rate_limiter.asyncio.sleep", new_callable=AsyncMock):
        result = await limiter.run_with_retry(
            coro_factory=sometimes_rate_limit,
            agent_name="test",
            logger=radar_logger,
        )
    assert result == "success"
    assert call_count == 3


@pytest.mark.asyncio
async def test_rate_limiter_exhausted_retries(radar_logger):
    limiter = AgentRateLimiter(max_concurrent=2, max_retries=1)

    async def always_rate_limit():
        raise RateLimitError("always limited")

    with patch("agents.group.rate_limiter.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RateLimitError):
            await limiter.run_with_retry(
                coro_factory=always_rate_limit,
                agent_name="test",
                logger=radar_logger,
            )

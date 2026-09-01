"""并发限流 + exponential backoff 重试"""
import asyncio
from utils.logger import ensure_radar


class RateLimitError(Exception):
    """LLM 限流错误"""
    pass


class AgentRateLimiter:
    def __init__(self, max_concurrent: int = 3, max_retries: int = 3):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.max_retries = max_retries

    async def run_with_retry(self, coro_factory, agent_name: str, logger=None):
        """带限流和重试执行协程

        Args:
            coro_factory: 返回协程的工厂函数（每次调用创建新协程）
            agent_name: agent 名称（用于日志）
            logger: 日志器
        """
        logger = ensure_radar(logger)
        last_error = None

        for attempt in range(self.max_retries + 1):
            async with self._semaphore:
                try:
                    return await coro_factory()
                except RateLimitError as e:
                    last_error = e
                    if attempt == self.max_retries:
                        logger.error("G", f"{agent_name} 限流重试耗尽 ({self.max_retries}次)")
                        raise
                    wait = 2 ** attempt
                    logger.warning("G", f"{agent_name} 限流，{wait}s 后重试 ({attempt+1}/{self.max_retries})")
                    await asyncio.sleep(wait)
                except Exception:
                    raise  # 非限流错误直接抛出

        raise last_error  # type: ignore

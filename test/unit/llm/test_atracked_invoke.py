"""atracked_invoke 测试：异步重试 + TokenRecorder 注入（用 fake 异步模型，不依赖真实 API）。"""
import asyncio
import logging

import pytest

from utils.llm_factory import atracked_invoke, TokenRecorder
from utils.token_recorder import TokenRecorder as TR


class _FakeResp:
    content = "ok"


class FakeAsyncLLM:
    """第一次调用抛瞬时错误（ConnectionError），第二次成功。"""

    def __init__(self):
        self.calls = 0
        self.last_config = None

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        if self.calls < 2:
            raise ConnectionError("transient")
        return _FakeResp()


def test_atracked_invoke_retries_and_records():
    model = FakeAsyncLLM()
    logger = logging.getLogger("test_atracked")

    async def run():
        return await atracked_invoke(model, [("user", "hi")], logger, label="t")

    resp = asyncio.run(run())
    assert resp.content == "ok"
    assert model.calls == 2  # 重试了一次
    # 回调里注入了 TokenRecorder（保证 token 追踪/预算生效）
    cbs = model.last_config.get("callbacks", [])
    assert any(isinstance(c, TokenRecorder) for c in cbs)


def test_atracked_invoke_non_retryable_raises():
    class FailLLM:
        async def ainvoke(self, messages, config=None):
            raise ValueError("fatal")

    async def run():
        return await atracked_invoke(FailLLM(), [("user", "x")], logging.getLogger("t"), label="t")

    with pytest.raises(ValueError):
        asyncio.run(run())

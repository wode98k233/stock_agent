"""LLM 重试与错误分类测试：ainvoke_with_retry / classify_llm_error。"""
import asyncio

import httpx
import openai
import pytest

from utils.llm_factory import classify_llm_error, ainvoke_with_retry


def _openai_err(exc_cls, message, status=429):
    """构造真实 openai 异常实例（需 httpx.Response 带 request）。"""
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return exc_cls(message, response=resp, body=None)


def _fake_rate_limit(message):
    return _openai_err(openai.RateLimitError, message, 429)


def _fake_timeout(message):
    return openai.APITimeoutError(httpx.Request("POST", "https://api.example.com/v1"))


def _fake_auth(message):
    return _openai_err(openai.AuthenticationError, message, 401)


def test_classify_quota():
    e = _fake_rate_limit(
        "Error code: 429 - {'message': 'Workspace allocated quota exceeded, "
        "please increase your quota limit.', 'code': 'insufficient_quota'}")
    assert classify_llm_error(e) == "quota"


def test_classify_rate_limit():
    e = _fake_rate_limit("Error code: 429 - rpm exhausted")
    assert classify_llm_error(e) == "rate_limit"


def test_classify_network():
    e = _fake_timeout("Connection timed out")
    assert classify_llm_error(e) == "network"


def test_classify_auth():
    e = _fake_auth("Incorrect API key provided")
    assert classify_llm_error(e) == "auth"


def test_classify_other():
    assert classify_llm_error(ValueError("x")) == "other"


# ── ainvoke_with_retry 重试逻辑 ──

class _FlakyLLM:
    """前 fail_count 次抛 429，之后成功。"""

    def __init__(self, fail_count):
        self.fail_count = fail_count
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise _fake_rate_limit("Error code: 429 - rpm exhausted")
        return "ok"


def test_retry_recovers_after_transient_429(monkeypatch):
    """429 重试后恢复：第 3 次成功（fail_count=2）。"""
    llm = _FlakyLLM(fail_count=2)

    async def fake_sleep(_):
        pass

    monkeypatch.setattr("utils.llm_factory.asyncio.sleep", fake_sleep)
    result = asyncio.run(ainvoke_with_retry(llm, ["m"], max_retries=5))
    assert result == "ok"
    assert llm.calls == 3  # 失败 2 次 + 成功 1 次


def test_retry_exhausted_raises_last_error(monkeypatch):
    """重试耗尽后抛出最后一个异常（不再无限重试）。"""
    llm = _FlakyLLM(fail_count=999)

    async def fake_sleep(_):
        pass

    monkeypatch.setattr("utils.llm_factory.asyncio.sleep", fake_sleep)
    with pytest.raises(Exception) as exc_info:
        asyncio.run(ainvoke_with_retry(llm, ["m"], max_retries=3))
    assert llm.calls == 4  # 初始 1 次 + 重试 3 次
    assert "429" in str(exc_info.value)


def test_retry_success_first_try():
    """无错误时一次成功，不重试。"""
    llm = _FlakyLLM(fail_count=0)
    result = asyncio.run(ainvoke_with_retry(llm, ["m"], max_retries=5))
    assert result == "ok"
    assert llm.calls == 1

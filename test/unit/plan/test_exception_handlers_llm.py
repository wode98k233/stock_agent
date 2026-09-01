"""Plan 异常处理链测试：LLM 服务不可用（配额/限流）时终止并给出明确提示，不调 LLM 总结。"""
import asyncio
import httpx
import openai
import pytest

from agents.plan.exception_handlers import (
    PlanGenericExceptionHandler,
    PlanHandlerContext,
)


def _quota_error():
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return openai.RateLimitError(
        "Error code: 429 - {'message': 'Workspace allocated quota exceeded, "
        "please increase your quota limit.', 'code': 'insufficient_quota'}",
        response=resp, body=None,
    )


class _Ctx:
    def __init__(self):
        self.completed_called = False
        self.trace_status = None

    def extract_completed_steps(self):
        self.completed_called = True  # 配额场景不应走到这
        return [("步骤1", "结果1")]

    def end_trace(self, result, status="success"):
        self.trace_status = status


class _Logger:
    def __init__(self):
        self.calls = []

    def error(self, *args, **kwargs):
        self.calls.append(("error", args, kwargs))

    def info(self, *args, **kwargs):
        self.calls.append(("info", args, kwargs))


@pytest.fixture
def handler():
    return PlanGenericExceptionHandler()


def _make_context():
    from types import SimpleNamespace
    return SimpleNamespace(
        logger=_Logger(),
        user_input="测试问题",
        run_ctx=_Ctx(),
        extract_completed_steps=lambda: [("步骤1", "结果1")],
    )


def test_quota_error_terminates_with_hint(handler):
    """配额不足 → 返回明确提示，且不调 LLM 生成总结（不触发 extract_completed_steps 的总结路径）。"""
    ctx = _make_context()
    result = asyncio.run(handler.handle(_quota_error(), ctx))
    assert "配额不足" in result
    assert "insufficient quota" in result.lower() or "quota" in result.lower()
    assert ctx.run_ctx.trace_status == "error"


def test_generic_error_still_uses_summary_path(handler, monkeypatch):
    """非 LLM 错误仍走原有总结恢复路径。"""
    ctx = _make_context()
    calls = {"summary": 0}

    # 模拟 generate_summary 被调用（成功路径，同步函数）
    def fake_generate(*args, **kwargs):
        calls["summary"] += 1
        return "生成的总结"

    import agents.shared.stream_utils as su
    monkeypatch.setattr(su, "generate_summary", fake_generate)
    result = asyncio.run(handler.handle(ValueError("普通错误"), ctx))
    assert calls["summary"] == 1
    assert result == "生成的总结"

"""工具压缩「首批预热」门闩测试。

背景：一轮内多条超长输出的压缩调用并发发出时全部冷启动（provider 缓存 miss）。
门闩让同一次 run 的第一条压缩先单独完成，其余等 event 后并行。

运行: pytest test/unit/react/test_tool_compress_warmup.py -v
"""
import asyncio
import logging
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _log():
    from utils.logger import ensure_radar
    return ensure_radar(logging.getLogger("test.compress_warmup"))


def _make_node():
    from agents.executor_callbacks import ExecutionState
    from agents.react.tool_node_base import BaseToolNode
    return BaseToolNode(exec_state=ExecutionState(), logger=_log())


async def test_first_compress_runs_solo(monkeypatch):
    """同 run 首条压缩独占执行；其余等 event 后并行。"""
    import agents.react.tool_node_base as tnb

    active = 0
    max_active_before_first_done = 0
    first_done = False

    async def fake_compress(tool_name, content, logger, user_goal=""):
        nonlocal active, max_active_before_first_done, first_done
        active += 1
        if not first_done:
            max_active_before_first_done = max(max_active_before_first_done, active)
        await asyncio.sleep(0.05)
        active -= 1
        first_done = True
        return f"c:{content}"

    monkeypatch.setattr(tnb, "_compress_tool_output", fake_compress)
    node = _make_node()
    results = await asyncio.gather(*[
        node.compress_tool_output("t", str(i)) for i in range(3)
    ])
    assert sorted(results) == ["c:0", "c:1", "c:2"]
    assert max_active_before_first_done == 1, "首条必须独占（不应有并发冷启动）"
    assert node.exec_state._compress_warm_event.is_set()


async def test_warm_skips_gate_and_runs_parallel(monkeypatch):
    """event 置位后的批次不再等待，直接并行。"""
    import agents.react.tool_node_base as tnb

    node = _make_node()

    async def fake_first(tool_name, content, logger, user_goal=""):
        return f"c:{content}"

    monkeypatch.setattr(tnb, "_compress_tool_output", fake_first)
    await node.compress_tool_output("t", "0")
    assert node.exec_state._compress_warm_event.is_set()

    active = 0
    max_active = 0

    async def fake_parallel(tool_name, content, logger, user_goal=""):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return f"c:{content}"

    monkeypatch.setattr(tnb, "_compress_tool_output", fake_parallel)
    results = await asyncio.gather(*[
        node.compress_tool_output("t", str(i)) for i in range(3)
    ])
    assert sorted(results) == ["c:0", "c:1", "c:2"]
    assert max_active == 3, "已暖后的批次应完全并行"


# ── 429 重试（2026-10-10 修）────────────────────────────────────────────
# 原实现直接 llm.ainvoke，遇 429 立刻吞掉回落原文 → provider 限流时压缩整批失效。
# 现在走 ainvoke_with_retry（full-jitter 指数退避），重试耗尽才回落。

def _rate_limit_error():
    """复现真实 429：openai SDK 把 HTTP 429 包成 RateLimitError。

    刻意用 openai 原生异常而非 httpx.HTTPStatusError —— 后者不携带
    status_code 属性，_is_retryable_error 判不出来，用它测会假绿。
    """
    import httpx
    import openai
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError(
        "Error code: 429 - {'error': {'message': 'rpm exhausted', "
        "'type': 'quota_exceeded_error', 'code': '8'}}",
        response=response, body=None,
    )


def _no_backoff(monkeypatch):
    """把重试退避时长压成 0。

    只 patch utils.llm_factory._retry_delay（退避时长计算），
    **不要**去 patch asyncio.sleep —— 那是全局模块，pytest-asyncio 的事件循环
    自身也依赖它，一改就把整个测试进程挂死。
    """
    monkeypatch.setattr("utils.llm_factory._retry_delay", lambda attempt: 0)


async def test_compress_retries_on_429(monkeypatch):
    """429 应触发重试，并在后续成功时返回压缩结果。"""
    import agents.react.utils as ru

    calls = {"n": 0}

    class _LLM:
        async def ainvoke(self, messages, config=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _rate_limit_error()
            return type("R", (), {"content": "COMPRESSED"})()

    monkeypatch.setattr(ru, "get_compress_llm", lambda: _LLM())
    _no_backoff(monkeypatch)

    out = await ru._compress_tool_output("iwc_query", "X" * 5000, _log())
    assert out == "COMPRESSED"
    assert calls["n"] == 2, "首次 429 后应重试一次并成功"


async def test_compress_falls_back_to_raw_after_retries_exhausted(monkeypatch):
    """重试耗尽仍 429 → 回落原文（压缩是可选优化，不能让工具执行失败）。"""
    import agents.react.utils as ru

    calls = {"n": 0}

    class _LLM:
        async def ainvoke(self, messages, config=None):
            calls["n"] += 1
            raise _rate_limit_error()

    monkeypatch.setattr(ru, "get_compress_llm", lambda: _LLM())
    _no_backoff(monkeypatch)

    raw = "Y" * 5000
    out = await ru._compress_tool_output("iwc_query", raw, _log())
    assert out == raw, "重试耗尽必须回落原文"
    assert calls["n"] == ru._TOOL_COMPRESS_MAX_RETRIES + 1, "首次 + N 次重试"


async def test_compress_does_not_retry_on_auth_error(monkeypatch):
    """401/额度类错误不可重试，立即回落（重试只会白等退避）。"""
    import agents.react.utils as ru

    calls = {"n": 0}

    class _LLM:
        async def ainvoke(self, messages, config=None):
            calls["n"] += 1
            raise ValueError("401 invalid api key")

    monkeypatch.setattr(ru, "get_compress_llm", lambda: _LLM())

    raw = "Z" * 5000
    out = await ru._compress_tool_output("iwc_query", raw, _log())
    assert out == raw
    assert calls["n"] == 1, "非瞬时错误不应重试"

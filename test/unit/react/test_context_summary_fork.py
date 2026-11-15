"""上下文压缩 fork + 软闸门 的回归护栏（PRD-A11 FR-2 / FR-4）。

要锁死四件事：

1. **fork 档用主模型** —— 跨 provider 缓存池不共享，用 sensenova 必然 0 命中；
2. **fork 形态**：压缩输入 = 已热前缀 + 尾部指令，**pending 全文不再重复发**；
3. **摘要六字段 schema 不变**，`_compose` 的覆盖窗口语义不变；
4. **软闸门无条件触发**，且 `compress_reason` 能区分触发原因。
"""
from __future__ import annotations

import json
import logging

import pytest

from agents.react.context import (ReactContextBuilder, default_summarizer,
                                  format_rounds_for_summary, split_react_rounds_with_spans)
from agents.shared.prompt_prefix import make_fork_prefix
from config import Config
from langchain_core.messages import AIMessage, ToolMessage


class _Exec:
    """最小 ExecutionState 替身：只要几个被 context.py 读写的属性。"""
    def __init__(self):
        self.react_context_summaries = []
        self.react_context_summarized_until = 0
        self.react_context_summary_updates = 0


_LOG = logging.getLogger("test.context_summary_fork")


def _rounds(n: int) -> list:
    """构造 n 个 ReAct 轮次（AI tool_calls + 对应 ToolMessage）。"""
    msgs = []
    for i in range(1, n + 1):
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "mx_data_query", "args": {"q": f"查询{i}"}, "id": f"c{i}"}]))
        msgs.append(ToolMessage(content=f"第{i}轮结果 " + "数据" * 200,
                                name="mx_data_query", tool_call_id=f"c{i}"))
    return msgs


def _summary(**over) -> dict:
    s = {"summary_version": 1, "summarized_rounds": [1, 2],
         "rounds_summary": "第1轮：查了指数", "key_findings": "上证 3250.45",
         "data_coverage": ["大盘行情"], "failed_queries": []}
    s.update(over)
    return s


# ── 1. fork 档必须用主模型（跨 provider 不共享缓存）──────────────────────
def test_fork_mode_uses_passed_llm_not_compress_model(monkeypatch):
    """fork 档必须忽略压缩模型，用调用方传入的 llm（即主模型）。"""
    sentinel = object()
    called = {}

    def fake_json(llm, messages, logger, **kw):
        called["llm"] = llm
        called["messages"] = messages
        called["kwargs"] = kw
        return _summary()

    monkeypatch.setattr("agents.react.context.llm_json_with_retry", fake_json)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "fork")

    default_summarizer(None, "pending", llm=sentinel, logger=None, max_chars=2500,
                       prior_count=0, current_count=2,
                       fork_prefix=make_fork_prefix(_rounds(2), [{"t": 1}]))
    assert called["llm"] is sentinel, "fork 档用了压缩模型 → 跨 provider，前缀必不命中"


def test_standalone_mode_still_uses_compress_model(monkeypatch):
    sentinel = object()
    called = {}

    def fake_json(llm, messages, logger, **kw):
        called["llm"] = llm
        return _summary()

    monkeypatch.setattr("agents.react.context.llm_json_with_retry", fake_json)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    monkeypatch.setattr("agents.react.context._summary_llm", lambda fb=None: "COMPRESS_MODEL")

    default_summarizer(None, "pending", llm=sentinel, logger=None, prior_count=0,
                       current_count=2)
    assert called["llm"] == "COMPRESS_MODEL"


# ── 2. fork 形态：尾部只放指令，pending 全文不重复发 ─────────────────────
def test_fork_payload_does_not_resend_pending_text(monkeypatch):
    rounds, _spans = split_react_rounds_with_spans(_rounds(2))
    pending_text = format_rounds_for_summary(rounds)
    called = {}

    def fake_json(llm, messages, logger, **kw):
        called["messages"] = messages
        called["kwargs"] = kw
        return _summary()

    monkeypatch.setattr("agents.react.context.llm_json_with_retry", fake_json)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "fork")

    default_summarizer(None, pending_text, llm=object(), logger=None, max_chars=2500,
                       prior_count=0, current_count=2,
                       fork_prefix=make_fork_prefix(rounds, [{"t": 1}]))

    msgs = called["messages"]
    # 前缀 = agent 已发过的完整 messages
    assert len(msgs) == len(rounds) + 1, "fork 形态应是「前缀 + 1 条尾部」"
    # 尾部是归档指令，且不含 pending 全文
    tail = str(msgs[-1].get("content") or msgs[-1][1])
    assert "上下文归档" in tail
    assert "R1..R2" in tail
    assert pending_text not in tail, "pending 全文已在前缀里，尾部再发一遍就是纯浪费"
    assert called["kwargs"]["fork_kind" if "fork_kind" in called["kwargs"] else "metadata"] \
        or True
    assert called["kwargs"]["metadata"]["fork_kind"] == "llm_fork"


def test_fork_falls_back_to_standalone_when_prefix_unusable(monkeypatch):
    """prefix 无 tools → 回落 standalone（不能静默发出半 fork 载荷）。"""
    called = {}

    def fake_json(llm, messages, logger, **kw):
        called["messages"] = messages
        called["kwargs"] = kw
        return _summary()

    monkeypatch.setattr("agents.react.context.llm_json_with_retry", fake_json)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "fork")

    default_summarizer(None, "PENDING", llm=object(), logger=None, prior_count=0,
                       current_count=1, fork_prefix=make_fork_prefix(_rounds(1), []))
    assert called["messages"][0][0] == "system"
    assert "PENDING" in called["messages"][1][1]
    assert called["kwargs"]["metadata"]["fork_kind"] == "standalone"
    assert called["kwargs"]["skip_cache_prefix"] is True


def test_standalone_payload_shape_unchanged(monkeypatch):
    called = {}

    def fake_json(llm, messages, logger, **kw):
        called["messages"] = messages
        return _summary()

    monkeypatch.setattr("agents.react.context.llm_json_with_retry", fake_json)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    default_summarizer(None, "PENDING", llm=object(), logger=None, prior_count=0,
                       current_count=1)
    assert called["messages"][0][0] == "system"      # SUMMARY_SYSTEM_PROMPT
    assert called["messages"][1][0] == "user"
    assert "PENDING" in called["messages"][1][1]


# ── 3. schema 与覆盖窗口语义不变 ────────────────────────────────────────
def test_schema_fields_unchanged_after_compression(monkeypatch):
    monkeypatch.setattr("agents.react.context.llm_json_with_retry",
                        lambda *a, **k: _summary())
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=2,
                            summary_pending_chars=10_000)
    ex = _Exec()
    out = b.build(messages=_rounds(3), user_input="今日大盘总结", exec_state=ex,
                  llm=object(), logger=_LOG)
    blob = json.dumps([s for s in ex.react_context_summaries], ensure_ascii=False)
    for field in ("summary_version", "summarized_rounds", "rounds_summary",
                  "key_findings", "data_coverage", "failed_queries"):
        assert field in blob, f"摘要缺字段 {field}"
    assert ex.react_context_summarized_until > 0
    # 被摘掉的轮次确实不在输出里了（摘要插在其位置）
    assert len(out) < 3 * 2


def test_compress_reason_recorded(monkeypatch):
    monkeypatch.setattr("agents.react.context.llm_json_with_retry",
                        lambda *a, **k: _summary())
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=2,
                            summary_pending_chars=10_000_000)
    ex = _Exec()
    b.build(messages=_rounds(3), user_input="q", exec_state=ex, llm=object(), logger=_LOG)
    assert ex.react_context_summaries[0]["compress_reason"] == "rounds"


def test_compress_reason_reaches_llm_step_metadata(monkeypatch):
    """闸门原因必须落到 LLM 步骤的 trace 里（FR-4 AC-3），否则事后分不清是哪种触发。"""
    captured = {}

    def fake_json(llm, messages, logger, **kw):
        captured.update(kw)
        return _summary()

    monkeypatch.setattr("agents.react.context.llm_json_with_retry", fake_json)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=2,
                            summary_pending_chars=10_000_000)
    b.build(messages=_rounds(3), user_input="q", exec_state=_Exec(),
            llm=object(), logger=_LOG)
    assert captured["metadata"]["compress_reason"] == "rounds"


# ── 4. 软闸门 ──────────────────────────────────────────────────────────
def test_soft_limit_triggers_even_below_thresholds(monkeypatch):
    """轮次/字符都没到，但预估 payload 超软上限 → 必须无条件压缩。"""
    monkeypatch.setattr("agents.react.context.llm_json_with_retry",
                        lambda *a, **k: _summary())
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    monkeypatch.setattr(Config, "REACT_CONTEXT_SOFT_LIMIT_TOKENS", 100)
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=99,
                            summary_pending_chars=10_000_000)
    ex = _Exec()
    b.build(messages=_rounds(3), user_input="q", exec_state=ex, llm=object(),
            logger=_LOG)
    assert len(ex.react_context_summaries) == 1
    assert "soft_limit" in ex.react_context_summaries[0]["compress_reason"]


def test_soft_limit_disabled_by_default(monkeypatch):
    monkeypatch.setattr("agents.react.context.llm_json_with_retry",
                        lambda *a, **k: _summary())
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    monkeypatch.setattr(Config, "REACT_CONTEXT_SOFT_LIMIT_TOKENS", 0)   # 关闭
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=99,
                            summary_pending_chars=10_000_000)
    ex = _Exec()
    b.build(messages=_rounds(3), user_input="q", exec_state=ex, llm=object(),
            logger=_LOG)
    assert ex.react_context_summaries == []


def test_reasons_can_combine(monkeypatch):
    monkeypatch.setattr("agents.react.context.llm_json_with_retry",
                        lambda *a, **k: _summary())
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    monkeypatch.setattr(Config, "REACT_CONTEXT_SOFT_LIMIT_TOKENS", 10)
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=2,
                            summary_pending_chars=1)
    ex = _Exec()
    b.build(messages=_rounds(3), user_input="q", exec_state=ex, llm=object(),
            logger=_LOG)
    r = ex.react_context_summaries[0]["compress_reason"]
    assert "soft_limit" in r and "rounds" in r and "chars" in r


def test_no_pending_means_no_compression(monkeypatch):
    monkeypatch.setattr("agents.react.context.llm_json_with_retry",
                        lambda *a, **k: _summary())
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    b = ReactContextBuilder(recent_rounds=5, summary_trigger_rounds=1,
                            summary_pending_chars=1)
    ex = _Exec()
    b.build(messages=_rounds(1), user_input="q", exec_state=ex, llm=object(),
            logger=_LOG)
    assert ex.react_context_summaries == []      # 全在 recent 窗口内 → 无 pending


def test_failure_does_not_advance_cursor(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider 挂了")
    monkeypatch.setattr("agents.react.context.llm_json_with_retry", boom)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARY_MODE", "standalone")
    b = ReactContextBuilder(recent_rounds=0, summary_trigger_rounds=2,
                            summary_pending_chars=10_000)
    ex = _Exec()
    out = b.build(messages=_rounds(3), user_input="q", exec_state=ex,
                  llm=object(), logger=_LOG)
    assert ex.react_context_summarized_until == 0
    assert len(out) == 6          # 全部轮次原样保留
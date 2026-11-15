"""clarify → task_spec 透传（不真正调 LLM）。

两条链路都要锁：
- ``classify_input`` 把 ``maybe_clarify`` 的返回值接进 common.py；
- ``maybe_clarify`` 自身在用户作答后**真的去抽取**任务结构，并把 question /
  answers_block 传对；抽取失败/全部跳过时不得影响放行。
"""
import asyncio
import json
import logging

import pytest

from agents.common import classify_input
from agents.shared import clarify as clarify_mod
from agents.shared.budget_ctx import budget_decision_ctx
from config import Config


class _Memory:
    enabled = False

    def get_history(self):
        return []


class _LLM:
    pass


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


_QUESTIONS = [{
    "id": "q1",
    "question": "关注哪个方向？",
    "options": [{"label": "半导体", "value": "半导体"},
                {"label": "消费", "value": "消费"}],
    "recommended": "半导体",
    "allow_custom": True,
}]


@pytest.fixture(autouse=True)
def _force_clarify_enabled(monkeypatch):
    """I1：maybe_clarify 读 Config.CLARIFY_ENABLED（来自 .env，默认 true）。

    显式固定为 True，避免本地 .env / CI 环境差异让下方直调用例静默走进
    「澄清未开启」的提前返回分支。
    """
    monkeypatch.setattr(Config, "CLARIFY_ENABLED", True)


def _run_maybe_clarify(monkeypatch, cb, extract=None, budget=None):
    """在 Web 决策通道下直调 maybe_clarify（raw_input / 问题生成均 mock）。"""
    monkeypatch.setattr(clarify_mod, "_generate_questions", lambda *a, **k: _QUESTIONS)
    monkeypatch.setattr("agents.shared.raw_input_ctx.get_raw_input",
                        lambda fallback="": "原始问题")
    if extract is not None:
        monkeypatch.setattr("agents.shared.task_spec.maybe_extract_task_spec", extract)
    token = budget_decision_ctx.set(cb)
    try:
        return asyncio.run(clarify_mod.maybe_clarify(
            "enriched 输入", memory=None, llm=object(), logger=_Logger(), budget=budget))
    finally:
        budget_decision_ctx.reset(token)


def test_classify_input_passes_task_spec(monkeypatch):
    async def fake_clarify(*a, **k):
        return {"answers": {"q1": "科技"}, "enriched_input": "问题+回答",
                "task_spec": {"entities": [{"key": "科技"}]}}

    # classify 先要过「是否股票相关」这一关；把它短路成 true
    monkeypatch.setattr("utils.llm_factory.llm_json_with_retry",
                        lambda *a, **k: {"is_stock_related": True})
    monkeypatch.setattr("agents.shared.clarify.maybe_clarify", fake_clarify)
    # 干净 checkout 没有 db/：策略会话查询显式短路，别让该用例依赖本地库；
    # logger 也不能传 None（异常回退路径会包成 RadarLogger(None) 崩掉）。
    monkeypatch.setattr("utils.cache.backtest_db.get_open_strategy_session",
                        lambda *a, **k: None)
    out = asyncio.run(classify_input(
        "问题", _Memory(), _LLM(), logging.getLogger("test")))
    assert out["task_spec"]["entities"][0]["key"] == "科技"
    assert out["clarified_input"] == "问题+回答"


# ── maybe_clarify 直调：抽取行为 ────────────────────────────

def test_maybe_clarify_extracts_task_spec(monkeypatch):
    """用户作答后必须直调抽取，question 用原话、answers_block 含作答文本。"""
    calls = []

    async def fake_extract(question, answers_block, llm, budget=None,
                           metadata=None, run_config=None):
        calls.append({
            "question": question,
            "answers_block": answers_block,
            "llm": llm,
            "budget": budget,
            "metadata": metadata,
            "run_config": run_config,
        })
        return {"entities": [{"key": "科技"}]}

    out = _run_maybe_clarify(
        monkeypatch,
        cb=lambda payload: json.dumps({"answers": {"q1": "半导体"}}),
        extract=fake_extract,
        budget=None,
    )

    assert out["task_spec"]["entities"][0]["key"] == "科技"
    assert out["enriched_input"]
    assert calls and calls[0]["question"] == "原始问题"
    assert "半导体" in calls[0]["answers_block"]
    assert calls[0]["budget"] is None


def test_maybe_clarify_extract_failure_still_returns_enriched_input(monkeypatch):
    """抽取抛异常 → 仍按澄清结果放行，且不得带 task_spec 键。"""
    async def boom(*a, **k):
        raise RuntimeError("抽取失败")

    out = _run_maybe_clarify(
        monkeypatch,
        cb=lambda payload: json.dumps({"answers": {"q1": "半导体"}}),
        extract=boom,
    )

    assert out["enriched_input"]
    assert "task_spec" not in out


def test_maybe_clarify_all_skipped_does_not_extract(monkeypatch):
    """全部跳过 = 没有可注入信息 → 提前放行，不得浪费一次抽取调用。"""
    calls = []

    async def fake_extract(*a, **k):
        calls.append(1)
        return {"entities": [{"key": "科技"}]}

    out = _run_maybe_clarify(
        monkeypatch,
        cb=lambda payload: json.dumps({"answers": {}, "skipped": ["q1"]}),
        extract=fake_extract,
    )

    assert out == {}
    assert calls == []

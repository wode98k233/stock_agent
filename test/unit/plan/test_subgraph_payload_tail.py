# -*- coding: utf-8 -*-
"""plan 系内层子图 payload 的尾部契约（回归测试，零 LLM 调用）。

背景：`cdf5ae5` 把内层 payload 从
    [system: 任务+规则+guidance+边界][history][user]
改成
    [REACT system][history][guidance][user][尾部]
时，**漏掉了 `system_prompt`（"执行以下任务：{step_purpose}" + 5 条执行规则）**
—— 任务与「目标达成判定」（反过度调用）整段从 payload 里消失。
本测试锁住"任务/规则必须在 payload 中"，防止再次静默丢失。

同时锁住"guidance 仍在 user 之前"（2026-10-17 A/B 实测：挪到尾部会让
planner 命中 41%→2%、unified 整 run 67.8%→60.2%，净负，已回滚）。
"""
import asyncio

import agents.common_react.graph as G
from agents.prompts import REACT_SYSTEM_PROMPT

USER_INPUT = "获取药明康德 最新股价和一条相关新闻"
TAIL_EXTRA = "【技能详情】mx_data 的工具详情…"
TEMPLATE_ID = "stock_deep_dive"


class _CaptureGraph:
    def __init__(self, box):
        self._box = box

    async def ainvoke(self, state, config=None):
        self._box["messages"] = state["messages"]
        return {"final_result": "captured"}


def _role(m):
    return str(m[0]) if isinstance(m, (tuple, list)) else str(getattr(m, "type", ""))


def _txt(m):
    return str(m[1]) if isinstance(m, (tuple, list)) else str(getattr(m, "content", "") or "")


def _capture_inner(monkeypatch):
    box = {}
    monkeypatch.setattr(G.ReactSubGraph, "_build_graph", lambda self: _CaptureGraph(box))
    from utils.logger import get_logger
    lg, *_ = get_logger("unit-subgraph-tail", skip_db=True)
    asyncio.run(G.run_react_subgraph(
        llm=None, tools=[], step_purpose="执行计划:\n 1. [mx_data] 查询股价",
        context_messages=[("user", USER_INPUT)], tail_system=TAIL_EXTRA,
        max_iterations=2, logger=lg, template_id=TEMPLATE_ID))
    return box["messages"]


def test_task_and_rules_present_in_payload(monkeypatch):
    """任务与执行规则必须还在 payload 里（回归：cdf5ae5 曾整段丢弃）。"""
    inner = _capture_inner(monkeypatch)
    joined = "\n".join(_txt(m) for m in inner)
    assert "执行以下任务" in joined
    assert "目标达成判定" in joined


def test_head_is_react_system_prompt(monkeypatch):
    """头部第一条保持 REACT system（前缀形态不能退化）。"""
    inner = _capture_inner(monkeypatch)
    assert _txt(inner[0]) == REACT_SYSTEM_PROMPT


def test_guidance_stays_before_user(monkeypatch):
    """guidance 必须仍在 user **之前**（挪尾部经 A/B 实测为净负，已回滚）。"""
    inner = _capture_inner(monkeypatch)
    joined = "\n".join(_txt(m) for m in inner)
    assert "场景化数据契约" in joined          # guidance 有投递
    roles = [_role(m) for m in inner]
    assert "user" in roles
    u_idx = roles.index("user")
    g_idx = next(i for i, m in enumerate(inner) if "场景化数据契约" in _txt(m))
    assert g_idx < u_idx, "guidance 不能跑到 user 之后"


def test_skill_detail_delivered_in_tail(monkeypatch):
    """技能详情投递不丢（尾部）。"""
    inner = _capture_inner(monkeypatch)
    joined = "\n".join(_txt(m) for m in inner)
    assert TAIL_EXTRA in joined

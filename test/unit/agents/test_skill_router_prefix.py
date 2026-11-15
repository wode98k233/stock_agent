"""skill-router / skill-gap 主前缀对齐回归（PRD-A11 同族口径）。

背景：这两条调用过去是 ``[("system", 指令)]`` 单条，与 agent 主调
``[tools][REACT system][*history][guidance][user][技能详情]`` 零字节重叠，
实测命中 1.7%~5%（CODE_WIKI §11.19）。改后形态::

    router/gap : [REACT][*history][guidance][user][system: 路由指令]
    agent 首调 : [REACT][*history][guidance][user][system: 技能详情]

**只往尾部追加、不改中间**：前缀段直接复用 prepare 已产出的消息，指令搬到尾部。
本文件锁定四条纪律：

1. 路由 payload 去掉最后一条后，必须与 agent 首调**逐字节相同**（sha 相同）；
2. 最后一条必须是 system 且含路由指令锚点；user 槽仍是用户问题；
3. 绑定的 tools 名称序列必须与 agent 那份一致（位置 0，换数组即前缀作废）；
4. 消息条数必须 > 1（证明真的挂了前缀，而不是退回单条 system）。
"""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

USER = "帮我看下贵州茅台最近的量价关系"


class _Logger:
    def isEnabledFor(self, *a, **k):
        return False

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeLLM:
    """只记录 bind_tools 的入参；不发起任何请求。"""

    def __init__(self):
        self.model_name = "stub"
        self.bound = None

    def bind_tools(self, tools):
        self.bound = list(tools)
        return self


class _Tool:
    def __init__(self, name):
        self.name = name


def _role(msg) -> str:
    if isinstance(msg, tuple):
        role = str(msg[0])
    else:
        role = str(getattr(msg, "type", "") or "")
    return {"human": "user", "ai": "assistant"}.get(role, role)


def _text(msg) -> str:
    if isinstance(msg, tuple):
        return str(msg[1])
    return str(getattr(msg, "content", "") or "")


def _digest(messages) -> str:
    """与 `_probes/probe_prefix_layout.py::messages_digest` 同口径（入口 `scripts/devtools/probe_prefix.py layout`）。"""
    raw = "\x01".join(
        json.dumps({"role": _role(m), "content": _text(m)},
                   ensure_ascii=False, separators=(",", ":"))
        for m in messages
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _agent_first_call(monkeypatch, history=None):
    """用真实的 PrepareNode 产出 agent 首调消息（与线上同一条装配路径）。"""
    from config import Config
    from agents.react.nodes import PrepareNode

    monkeypatch.setattr(Config, "REPORT_ENABLE_ANALYSIS_ENGINE", False)
    memory = SimpleNamespace(enabled=True, get_history=lambda: list(history or []))
    node = PrepareNode(memory=memory, skill_registry=None, tools=[])
    state = {
        "user_input": USER,
        "selected_template": None,
        "selected_template_id": "standard",
        "loaded_skills": [],
        "active_skill": "",
    }
    return asyncio.run(node(state))["messages"]


def _capture_router(monkeypatch, state, tools):
    """拦截 LLM 入口，返回 SubGoalRouterNode 实际发出的 messages 与绑定的 llm。"""
    from agents.react import skill_nodes

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        captured["llm"] = llm
        return {"active_skill": "a", "loaded_skills": ["a"], "sub_goals": []}

    monkeypatch.setattr(skill_nodes, "llm_json_with_retry", _fake)
    fake_llm = _FakeLLM()
    node = skill_nodes.SubGoalRouterNode(
        llm=fake_llm, skill_registry=None, tools_ref=tools,
        budget=None, logger=_Logger(), full_surface=True, activate_mode="model",
    )
    asyncio.run(node._route_with_llm(state, "a", ["a", "b"], [], [], None))
    return captured["messages"], captured["llm"]


def test_router_prefix_is_byte_identical_to_agent_first_call(monkeypatch):
    agent_msgs = _agent_first_call(monkeypatch)
    state = {
        "user_input": USER,
        "messages": list(agent_msgs),
        "candidate_skills": ["a", "b"],
        "active_skill": "a",
        "loaded_skills": [],
        "sub_goals": [],
        "iteration_count": 1,
        "skill_gap_count": 0,
    }
    tools = [_Tool("tool_1"), _Tool("tool_2"), _Tool("tool_3")]
    router_msgs, llm = _capture_router(monkeypatch, state, tools)

    # ① 前缀段（去掉尾部指令）与 agent 首调逐字节相同 —— 缓存命中的充要条件
    prefix = router_msgs[:-1]
    assert [_role(m) for m in prefix] == [_role(m) for m in agent_msgs]
    assert [_text(m) for m in prefix] == [_text(m) for m in agent_msgs]
    router_sha, agent_sha = _digest(prefix), _digest(agent_msgs)
    print(f"\nrouter-prefix sha={router_sha}\nagent-first-call sha={agent_sha}")
    assert router_sha == agent_sha

    # ② 尾部是 system 路由指令；user 槽仍是用户问题（没有被塞回 system）
    assert _role(router_msgs[-1]) == "system"
    assert "你是技能路由员" in _text(router_msgs[-1])
    assert [_role(m) for m in router_msgs] == ["system", "user", "system"]
    assert _text(router_msgs[1]) == USER

    # ③ 消息数比原实现（单条 system）多 ≥2 —— 证明真的挂了主前缀
    assert len(router_msgs) >= 3

    # ④ tools 与 agent 那份同源同序（位置 0，数组一变前缀归零）
    assert [t.name for t in llm.bound] == [t.name for t in tools]


def test_gap_router_uses_the_same_prefix_shape(monkeypatch):
    """SkillGapNode._pick_from_catalog 与 router 同形，避免一处改一处没改。"""
    from agents.react import skill_nodes
    from tools.skills import SkillPromptBuilder

    agent_msgs = _agent_first_call(monkeypatch)
    state = {
        "user_input": USER,
        "messages": list(agent_msgs),
        "candidate_skills": ["a"],
        "active_skill": "a",
        "loaded_skills": ["a"],
        "sub_goals": [],
        "iteration_count": 1,
        "skill_gap_count": 1,
    }
    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        return {"active_skill": "b", "reason": "test"}

    monkeypatch.setattr(skill_nodes, "llm_json_with_retry", _fake)
    monkeypatch.setattr(SkillPromptBuilder, "build_catalog_prompt",
                        classmethod(lambda cls, registry: "SKILL-CATALOG"))
    registry = SimpleNamespace(get_skill_catalog=lambda: [{"skill_name": "b"}])
    tools = [_Tool("tool_1")]
    node = skill_nodes.SkillGapNode(
        llm=_FakeLLM(), skill_registry=registry, tools_ref=tools,
        budget=None, logger=_Logger(), full_surface=True, activate_mode="model",
    )
    asyncio.run(node._pick_from_catalog(state, "a"))

    msgs = captured["messages"]
    assert _digest(msgs[:-1]) == _digest(agent_msgs)
    assert _role(msgs[-1]) == "system"
    assert "工具连续失败" in _text(msgs[-1])
    assert len(msgs) >= 3

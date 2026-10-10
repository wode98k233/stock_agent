"""子目标路由 / 失败条件边 / gap 重选。"""
import asyncio
import logging

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agents.react.nodes import SelectSkillsNode
from agents.react.skill_nodes import SubGoalRouterNode, SkillGapNode, make_route_after_tools


@pytest.fixture(autouse=True)
def _batch_one(monkeypatch):
    """本文件聚焦「切换/兜底」语义，用 batch=1 保持单技能（批量默认 3，另测）。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 1, raising=False)


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = name


class _Meta:
    def __init__(self, tools):
        self.tools = tools


class _Registry:
    def __init__(self):
        self._tools = {"a": [_Tool("a_tool")], "b": [_Tool("b_tool")]}

    def get_skill_catalog(self):
        return [
            {"skill_name": "a", "description": "A", "category": "c", "key_params": [],
             "target": "", "tool_names": ["a_tool"]},
            {"skill_name": "b", "description": "B", "category": "c", "key_params": [],
             "target": "", "tool_names": ["b_tool"]},
        ]

    def get_skill_meta(self, name):
        tools = self._tools.get(name)
        return _Meta(list(tools)) if tools is not None else None

    def get_tools(self, name):
        return list(self._tools.get(name, []))

    def get_all_tools(self):
        return [t for tools in self._tools.values() for t in tools]


class _AI:
    def __init__(self, n, name=None):
        from langchain_core.messages import AIMessage
        self._msg = AIMessage(content="", tool_calls=[
            {"name": name or f"t{i}", "args": {}, "id": f"c{i}"} for i in range(n)
        ])

    def __getattr__(self, item):
        return getattr(self._msg, item)


def _fail_msg(call_id="c0"):
    return ToolMessage(content="执行失败: boom", tool_call_id=call_id)


def _ok_msg(call_id="c0"):
    return ToolMessage(content="ok 结果", tool_call_id=call_id)


def _state(**kw):
    base = {"user_input": "分析茅台", "active_skill": "a", "candidate_skills": ["a", "b"],
            "sub_goals": [], "skill_gap_count": 0, "messages": []}
    base.update(kw)
    return base


def test_router_keeps_single_candidate_without_llm(monkeypatch):
    ref = []
    node = SubGoalRouterNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                             logger=logging.getLogger("t"))
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    out = asyncio.run(node(_state(active_skill="", candidate_skills=["a"])))
    assert out["active_skill"] == "a"
    assert [t.name for t in ref] == ["a_tool"]


def test_router_switches_via_llm(monkeypatch):
    ref = [type("T", (), {"name": "a_tool"})()]
    node = SubGoalRouterNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                             logger=logging.getLogger("t"))
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: {"active_skill": "b", "sub_goals": ["拿技术指标"]})
    out = asyncio.run(node(_state()))
    assert out["active_skill"] == "b"
    assert out["sub_goals"] == ["拿技术指标"]
    assert [t.name for t in ref] == ["b_tool"]


def test_router_invalid_output_keeps_active(monkeypatch):
    ref = []
    node = SubGoalRouterNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                             logger=logging.getLogger("t"))
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry", lambda *a, **k: None)
    out = asyncio.run(node(_state()))
    assert out["active_skill"] == "a"


def test_route_after_tools_all_failed_goes_gap():
    state = _state(messages=[_AI(2, name="a_tool"), _fail_msg("c0"), _fail_msg("c1")])
    assert make_route_after_tools(None)(state) == "gap"


def test_route_after_tools_partial_success_goes_agent():
    state = _state(messages=[_AI(2, name="a_tool"), _ok_msg("c0"), _fail_msg("c1")])
    assert make_route_after_tools(None)(state) == "agent"


def test_route_after_tools_duplicate_same_signature_failures_go_gap():
    """同轮两个同签名调用都失败（ToolMessage 均可判定失败）→ gap。"""
    state = _state(messages=[_AI(2, name="a_tool"), _fail_msg("c0"), _fail_msg("c1")])
    assert make_route_after_tools(None)(state) == "gap"


def test_route_after_tools_deduped_copied_failure_goes_gap():
    """去重场景：两个 tool_calls 仅一个失败 ToolMessage 被复制成两条 → gap。"""
    state = _state(messages=[_AI(2, name="a_tool"), _fail_msg("c0"), _fail_msg("c0")])
    assert make_route_after_tools(None)(state) == "gap"


def test_route_after_tools_one_success_one_failure_goes_agent():
    state = _state(messages=[_AI(2, name="a_tool"), _ok_msg("c0"), _fail_msg("c1")])
    assert make_route_after_tools(None)(state) == "agent"


def test_route_after_tools_cap_reached_goes_agent():
    state = _state(messages=[_AI(1), _fail_msg("c0")], skill_gap_count=2)
    assert make_route_after_tools(None)(state) == "agent"


def test_route_after_tools_default_limit_follows_skill_gap_node(monkeypatch):
    monkeypatch.setattr(SkillGapNode, "MAX_GAPS", 1)
    state = _state(messages=[_AI(1), _fail_msg("c0")], skill_gap_count=1)
    assert make_route_after_tools(None)(state) == "agent"


def test_gap_selects_next_candidate_and_records(monkeypatch):
    ref = [type("T", (), {"name": "a_tool"})()]
    events = []
    node = SkillGapNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                        logger=logging.getLogger("t"))
    monkeypatch.setattr("agents.react.skill_nodes.get_active_recorder",
                        lambda: type("R", (), {"record_skill_event": lambda self, k, d: events.append((k, d))})())
    out = asyncio.run(node(_state(messages=[_AI(1)])))
    assert out["active_skill"] == "b"
    assert out["skill_gap_count"] == 1
    assert [t.name for t in ref] == ["b_tool"]
    assert events and events[0][0] == "gap_reselect"
    assert events[0][1]["from"] == "a" and events[0][1]["to"] == "b"


def test_gap_skips_unknown_candidate_and_picks_valid(monkeypatch):
    """候选池含未注册技能名时跳过，选下一个有效候选。"""
    ref = [type("T", (), {"name": "a_tool"})()]
    events = []
    node = SkillGapNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                        logger=logging.getLogger("t"))
    monkeypatch.setattr("agents.react.skill_nodes.get_active_recorder",
                        lambda: type("R", (), {"record_skill_event": lambda self, k, d: events.append((k, d))})())
    out = asyncio.run(node(_state(candidate_skills=["a", "ghost", "b"])))
    assert out["active_skill"] == "b"
    assert out["skill_gap_count"] == 1
    assert [t.name for t in ref] == ["b_tool"]


def test_gap_catalog_pick_rejects_unknown_skill(monkeypatch):
    """全目录重选：LLM 返回目录外技能名视为无效，只记 gap 事件。"""
    ref = []
    events = []
    node = SkillGapNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                        logger=logging.getLogger("t"))
    monkeypatch.setattr("agents.react.skill_nodes.get_active_recorder",
                        lambda: type("R", (), {"record_skill_event": lambda self, k, d: events.append((k, d))})())
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: {"active_skill": "ghost"})
    out = asyncio.run(node(_state(candidate_skills=[])))
    assert "active_skill" not in out
    assert out["skill_gap_count"] == 1
    assert ref == []
    assert events and events[0][0] == "gap_reselect" and events[0][1]["succeeded"] is False


def test_chain_select_router_gap_switches(monkeypatch):
    """链路组合（不调真实 LLM）：select → router 激活 → 失败路由 → gap 切 B。"""
    ref = []
    registry = _Registry()

    monkeypatch.setattr("agents.react.nodes.llm_json_with_retry",
                        lambda *a, **k: {"selected_skills": ["a", "b"], "reason": "r"})
    select_node = SelectSkillsNode(llm=object(), skill_registry=registry,
                                   tools_ref=ref, logger=logging.getLogger("t"))
    out = asyncio.run(select_node({"user_input": "分析茅台", "selected_template": None}))
    assert out["active_skill"] == "a"
    assert out["candidate_skills"] == ["a", "b"]
    assert [t.name for t in ref] == ["a_tool"]

    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    router = SubGoalRouterNode(llm=object(), skill_registry=registry, tools_ref=ref,
                               logger=logging.getLogger("t"))
    rout = asyncio.run(router(_state(active_skill="", candidate_skills=["a"])))
    assert rout["active_skill"] == "a"
    assert [t.name for t in ref] == ["a_tool"]

    fail_msgs = [_AI(1, name="a_tool"), _fail_msg("c0")]
    assert make_route_after_tools(None)(_state(messages=fail_msgs)) == "gap"

    events = []
    monkeypatch.setattr("agents.react.skill_nodes.get_active_recorder",
                        lambda: type("R", (), {"record_skill_event": lambda self, k, d: events.append((k, d))})())
    gap = SkillGapNode(llm=object(), skill_registry=registry, tools_ref=ref,
                       logger=logging.getLogger("t"))
    gout = asyncio.run(gap(_state(messages=fail_msgs, skill_gap_count=0)))
    assert gout["active_skill"] == "b"
    assert gout["skill_gap_count"] == 1
    assert [t.name for t in ref] == ["b_tool"]
    assert events[0][0] == "gap_reselect"


# ── Step2：周期性换批（reselect） ──

def test_route_after_tools_reselects_when_due(monkeypatch):
    """还有未装载候选且到达间隔轮次 → reselect。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_RESELECT_EVERY", 1, raising=False)
    route = make_route_after_tools(None)
    st = _state(iteration_count=1, loaded_skills=["a"], candidate_skills=["a", "b"],
                messages=[_ok_msg()])
    assert route(st) == "reselect"


def test_route_after_tools_no_reselect_when_all_loaded(monkeypatch):
    """候选已全部装载 → 不再换批。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_RESELECT_EVERY", 1, raising=False)
    route = make_route_after_tools(None)
    st = _state(iteration_count=1, loaded_skills=["a", "b"], candidate_skills=["a", "b"],
                messages=[_ok_msg()])
    assert route(st) == "agent"


def test_reselect_appends_usage_for_added_skills(monkeypatch):
    """中途换批：LLM 决定新增技能 → 重装工具 + 追加新增技能的 L1 用法 system 消息。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 2, raising=False)
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: {"active_skill": "a", "loaded_skills": ["a", "b"]})
    monkeypatch.setattr("tools.skills.SkillPromptBuilder.build_tools_detail_prompt",
                        lambda reg, sk: f"USAGE::{sk}")

    ref = [_Tool("a_tool")]
    node = SubGoalRouterNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                             logger=logging.getLogger("t"), append_usage=True)
    state = _state(active_skill="a", candidate_skills=["a", "b"], loaded_skills=["a"],
                   iteration_count=2)
    out = asyncio.run(node(state))

    assert out["loaded_skills"] == ["a", "b"]
    assert [t.name for t in ref] == ["a_tool", "b_tool"]
    assert out.get("messages"), "新增技能应追加 L1 用法消息"
    assert "USAGE::b" in out["messages"][0][1]


def test_reselect_no_usage_message_when_unchanged(monkeypatch):
    """批次未变时不追加用法消息。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 2, raising=False)
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: {"active_skill": "a", "loaded_skills": ["a"]})
    ref = [_Tool("a_tool")]
    node = SubGoalRouterNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                             logger=logging.getLogger("t"), append_usage=True)
    state = _state(active_skill="a", candidate_skills=["a", "b"], loaded_skills=["a"],
                   iteration_count=1)
    out = asyncio.run(node(state))
    assert out["loaded_skills"] == ["a"]
    assert "messages" not in out


def test_router_catalog_fallback_adds_skill_outside_pool(monkeypatch):
    """候选池都不合适时，router 可回退到全量目录补一个技能，并并入候选池。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 3, raising=False)

    class _RegWithC(_Registry):
        def get_skill_catalog(self):
            return super().get_skill_catalog() + [
                {"skill_name": "c", "description": "C技能", "category": "c",
                 "key_params": [], "target": "", "tool_names": ["c_tool"]}]

        def get_skill_meta(self, name):
            if name == "c":
                return _Meta([_Tool("c_tool")])
            return super().get_skill_meta(name)

        def get_tools(self, name):
            if name == "c":
                return [_Tool("c_tool")]
            return super().get_tools(name)

    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: {"active_skill": "c", "loaded_skills": ["a", "c"]})
    ref = [_Tool("a_tool")]
    node = SubGoalRouterNode(llm=object(), skill_registry=_RegWithC(), tools_ref=ref,
                             logger=logging.getLogger("t"), allow_catalog_fallback=True)
    state = _state(active_skill="a", candidate_skills=["a", "b"], loaded_skills=["a"])
    out = asyncio.run(node(state))

    assert "c" in out["candidate_skills"]          # 补入候选池
    assert out["loaded_skills"] == ["a", "c"]
    assert [t.name for t in ref] == ["a_tool", "c_tool"]
    assert out["active_skill"] == "c"


def test_router_rejects_skill_outside_catalog_without_fallback(monkeypatch):
    """未开启兜底时，池外技能一律拒绝（保持旧行为）。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 3, raising=False)
    monkeypatch.setattr("agents.react.skill_nodes.llm_json_with_retry",
                        lambda *a, **k: {"active_skill": "ghost", "loaded_skills": ["ghost"]})
    ref = [_Tool("a_tool")]
    node = SubGoalRouterNode(llm=object(), skill_registry=_Registry(), tools_ref=ref,
                             logger=logging.getLogger("t"))
    state = _state(active_skill="a", candidate_skills=["a", "b"], loaded_skills=["a"])
    out = asyncio.run(node(state))
    assert out["active_skill"] == "a"
    assert "ghost" not in out["candidate_skills"]

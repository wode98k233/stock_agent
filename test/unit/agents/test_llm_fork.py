"""fork 原语的硬约束回归护栏（PRD-A11 FR-1 AC-1，零 LLM 调用）。

每条用例对应 docstring 里的一条约束。**任何为了让测试通过而放松约束的改动都不允许**——
这些约束一旦破坏，命中会静默归零，且事后从指标上看不出来。
"""
from __future__ import annotations

import pytest

from agents.shared.llm_fork import (MAX_TAIL_MESSAGES, build_fork_payload, fork_metadata,
                                    normalize_tail)
from agents.shared.prompt_prefix import (ForkPrefix, build_core_messages, make_fork_prefix,
                                         message_id, take_fork_prefix, with_tail_message)


def _prefix(n_tools: int = 2, source: str = "exec_state") -> ForkPrefix:
    msgs = [{"role": "system", "content": f"SYS-{i}", "id": f"s-{i}"} for i in range(3)]
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(n_tools)]
    return make_fork_prefix(msgs, tools, source=source)


# ── 约束 1：能力判定而非开关 ────────────────────────────────────────────
def test_none_prefix_returns_none():
    assert build_fork_payload(None, "尾部") is None


def test_prefix_without_tools_returns_none():
    p = ForkPrefix(messages=[{"role": "system", "content": "x"}], tools=[])
    assert build_fork_payload(p, "尾部") is None
    assert p.skip_reason() == "no_tools"


def test_prefix_without_messages_returns_none():
    p = ForkPrefix(messages=[], tools=[{"a": 1}])
    assert build_fork_payload(p, "尾部") is None
    assert p.skip_reason() == "no_messages"


def test_empty_tail_returns_none():
    assert build_fork_payload(_prefix(), "") is None
    assert build_fork_payload(_prefix(), None) is None


# ── 约束 2：必须绑定同一份 tools 数组 ──────────────────────────────────
def test_same_tools_are_returned_and_not_rebuilt():
    p = _prefix(n_tools=5)
    msgs, tools = build_fork_payload(p, "尾部")
    assert tools == p.tools
    assert len(tools) == 5
    # 返回的是快照，改返回值不影响原前缀
    tools.append({"x": 1})
    assert len(p.tools) == 5


def test_tools_are_copied_not_aliased():
    p = _prefix()
    _, tools = build_fork_payload(p, "尾部")
    tools.clear()
    assert len(p.tools) == 2


# ── 约束 3：只允许尾部追加 ──────────────────────────────────────────────
def test_prefix_messages_are_prefix_of_result_untouched():
    p = _prefix()
    before = [dict(m) for m in p.messages]
    msgs, _ = build_fork_payload(p, "尾部指令")
    assert msgs[: len(p.messages)] == before
    assert len(msgs) == len(p.messages) + 1


def test_source_messages_not_mutated():
    src = [{"role": "system", "content": "S"}]
    p = make_fork_prefix(src, [{"t": 1}])
    build_fork_payload(p, "尾部")
    assert src == [{"role": "system", "content": "S"}]


# ── 约束 4：禁止用 messages[0][0] != "system" 判 fork ───────────────────
def test_fork_result_starts_with_system_so_old_heuristic_is_wrong():
    """历史事故：调用方用 messages[0][0] != "system" 判断 fork，fork 后首条恰是
    agent 的 system → 误判成「未 fork」→ 漏绑 tools → 命中 67% 掉到 2.2%。
    这里把该误判固化成用例。"""
    p = _prefix()
    msgs, tools = build_fork_payload(p, "尾部")
    assert msgs[0]["role"] == "system"          # 旧启发式会说"这不是 fork"
    assert tools                                  # 真相是 fork 成功了
    assert fork_metadata(p, (msgs, tools))["fork_kind"] == "llm_fork"


# ── 约束 5：尾部角色默认 user；tail 形态与上限 ──────────────────────────
def test_tail_role_defaults_to_user():
    msgs, _ = build_fork_payload(_prefix(), "做一件事")
    assert msgs[-1]["role"] == "user"


def test_tail_role_overridable():
    msgs, _ = build_fork_payload(_prefix(), "做一件事", tail_role="system")
    assert msgs[-1]["role"] == "system"


def test_tail_list_supported_up_to_limit():
    """D1 决议：支持 list（压缩需要「指令 + 轮次锚点」两条），但上限 2 条。"""
    msgs, _ = build_fork_payload(_prefix(), ["指令", {"role": "system", "content": "锚点"}])
    assert msgs[-2]["role"] == "user"
    assert msgs[-1]["role"] == "system"
    assert len(msgs) == 3 + 2


def test_tail_list_over_limit_raises():
    with pytest.raises(ValueError, match=str(MAX_TAIL_MESSAGES)):
        build_fork_payload(_prefix(), ["a", "b", "c"])


def test_normalize_tail_dict_gets_default_role():
    items = normalize_tail({"content": "无角色"}, tail_role="user")
    assert items[0]["role"] == "user"
    assert items[0]["content"] == "无角色"


def test_normalize_tail_dict_respects_explicit_role():
    items = normalize_tail({"role": "system", "content": "锚点"}, tail_role="user")
    assert items[0]["role"] == "system"


def test_normalize_tail_drops_blank_content():
    assert normalize_tail("   ") == []
    assert normalize_tail([{"content": ""}]) == []


def test_normalize_tail_rejects_unknown_element_type():
    with pytest.raises(TypeError):
        normalize_tail([123])


def test_normalize_tail_rejects_unknown_type():
    with pytest.raises(TypeError):
        normalize_tail(123)


# ── 稳定性：tail 内容重复时 id 恒等（add_messages 幂等的前提）────────────
def test_tail_messages_have_stable_ids():
    a = with_tail_message([], "user", "同样的尾巴")
    b = with_tail_message([], "user", "同样的尾巴")
    assert a[0]["id"] == b[0]["id"]
    assert a[0]["id"] == message_id("tail", "同样的尾巴")


def test_with_tail_message_returns_new_list():
    src = [{"role": "system", "content": "S"}]
    out = with_tail_message(src, "user", "T")
    assert len(src) == 1 and len(out) == 2


# ── 埋点 ────────────────────────────────────────────────────────────────
def test_fork_metadata_for_forked_call():
    p = _prefix(n_tools=5)
    payload = build_fork_payload(p, "尾部")
    meta = fork_metadata(p, payload)
    assert meta["fork_kind"] == "llm_fork"
    assert meta["fork_prefix_msgs"] == 3
    assert meta["fork_tools"] == 5
    assert meta["fork_source"] == "exec_state"
    assert meta["fork_prefix_chars"] > 0


def test_fork_metadata_for_skipped_call_carries_reason():
    p = ForkPrefix(messages=[{"role": "system", "content": "x"}], tools=[])
    meta = fork_metadata(p, None)
    assert meta["fork_kind"] == "standalone"
    assert meta["fork_skip_reason"] == "no_tools"


def test_fork_metadata_without_prefix():
    meta = fork_metadata(None, None)
    assert meta == {"fork_kind": "none", "fork_skip_reason": "no_prefix"}


# ── 前缀采集/消费 ───────────────────────────────────────────────────────
class _Exec:
    pass


def test_capture_then_take_roundtrip():
    ex = _Exec()
    p = make_fork_prefix([{"role": "system", "content": "S"}], [{"t": 1}])
    got = take_fork_prefix(exec_state=ex)
    assert got is None                       # 还没采集

    from agents.shared.prompt_prefix import capture_fork_prefix
    capture_fork_prefix(ex, p.messages, p.tools, source="self")
    got = take_fork_prefix(exec_state=ex)
    assert got is not None and got.ok and got.source == "exec_state"


def test_capture_without_tools_returns_none_and_writes_nothing():
    """force_summary 模式（不绑工具）必须留空，否则下游拿到"半 fork"载荷。"""
    ex = _Exec()
    from agents.shared.prompt_prefix import capture_fork_prefix
    assert capture_fork_prefix(ex, [{"role": "system", "content": "S"}], []) is None
    assert take_fork_prefix(exec_state=ex) is None


def test_take_falls_back_to_state():
    state = {"_fork_messages": [{"role": "system", "content": "S"}],
             "_fork_tools": [{"t": 1}]}
    got = take_fork_prefix(exec_state=None, state=state)
    assert got is not None and got.source == "state"


def test_take_prefers_exec_state_over_state():
    ex = _Exec()
    from agents.shared.prompt_prefix import capture_fork_prefix
    capture_fork_prefix(ex, [{"role": "system", "content": "FROM_EXEC"}], [{"t": 9}])
    state = {"_fork_messages": [{"role": "system", "content": "FROM_STATE"}],
             "_fork_tools": [{"t": 1}]}
    got = take_fork_prefix(exec_state=ex, state=state)
    assert got.source == "exec_state"
    assert got.messages[0]["content"] == "FROM_EXEC"


def test_take_returns_none_when_both_empty():
    assert take_fork_prefix(exec_state=None, state={}) is None
    assert take_fork_prefix(exec_state=_Exec(), state=None) is None


# ── 主前缀构造器：纯函数 + 稳定 id ──────────────────────────────────────
def test_build_core_messages_is_pure_and_stable():
    a = build_core_messages()
    b = build_core_messages()
    assert a == b
    assert all("id" in m for m in a), "缺稳定 id → add_messages 重跑会追加重复消息"
    ids = [m["id"] for m in a]
    assert len(ids) == len(set(ids))


def test_core_messages_last_is_react_system():
    from agents.prompts import REACT_SYSTEM_PROMPT
    msgs = build_core_messages()
    assert msgs[-1]["content"] == REACT_SYSTEM_PROMPT


def test_prefix_chars_counts_dict_and_tuple_messages():
    p = make_fork_prefix(
        [{"role": "system", "content": "12345"}, ("system", "123")], [{"t": 1}])
    assert p.prefix_chars() == 8

# ── 主前缀构造器：两侧必须逐字节同形（PRD-A11 AC-11）────────────────────
def test_main_prefix_matches_agent_shape_after_compaction_merge():
    """构造器产出的骨架 == 压缩链路 `_merge_leading_system` 后的骨架。

    **这条锁的是一个真实事故**（2026-10-16）：共享构造器做好了，agent#1 仍只命中 50%。
    查消息构成发现：
      · agent 侧走压缩 → 头部 [REACT][guidance] 被合并成**一条** 3,118 字符
      · select_skills 侧不走压缩 → 保留 [REACT][guidance] **两条**
      → JSON 骨架不同 → 前缀从第 2 条消息起失效，fork 白做。

    所以构造器必须自己做同样的合并；两侧任何一方改动都会让这条用例变红。
    """
    from agents.prompts import REACT_SYSTEM_PROMPT
    from agents.react.context import _merge_leading_system
    from agents.shared.prompt_prefix import build_main_prefix_messages

    guidance = "## 场景化数据契约（必须遵守）\n- 场景：速览"
    user = "[背景信息]\n当前时间…\n[用户问题]\n获取药明康德最新股价"
    built = build_main_prefix_messages(history=[], guidance=guidance, user_input=user)

    # 模拟 agent 侧：prepare 的 messages 再经压缩链路的头部合并
    agent_side = _merge_leading_system([("system", REACT_SYSTEM_PROMPT),
                                        ("system", guidance), ("user", user)])
    assert built == agent_side, (
        "构造器与压缩链路的消息骨架不一致 → 前缀从分叉点失效\n"
        f"构造器: {[(r, len(c)) for r, c in built]}\n"
        f"agent : {[(r, len(c)) for r, c in agent_side]}"
    )


def test_main_prefix_merges_leading_system_messages():
    """头部连续 system 必须合并成一条（与 agent 侧同规则）。"""
    from agents.shared.prompt_prefix import build_main_prefix_messages, merge_leading_system

    msgs = build_main_prefix_messages(history=[], guidance="G", user_input="U")
    assert len(msgs) == 2, f"应为 [合并后的 system][user]，实际 {len(msgs)} 条"
    assert msgs[0][0] == "system" and "G" in msgs[0][1] and msgs[-1] == ("user", "U")
    # 通用行为：没有 system 时原样返回
    assert merge_leading_system([("user", "x")]) == [("user", "x")]

"""usage_guide 尾部投递（PRD-A9 §2.3.3）单元测试

硬约束：**只许尾部追加**。任何中间插入或改写都会破坏已建立的缓存前缀
（缓存只对「从头开始的连续前缀」生效，PRD §0.1）。
"""
import pytest

from agents.shared.usage_guide import SOFT_LIMIT_SKILLS, append_usage_guide, build_guide_text


class _FakeRegistry:
    def __init__(self, with_guide=True):
        self.with_guide = with_guide

    def get_skill_usage_guide(self, name):
        return f"## {name} 的用法\n口径：主源 X，回落 Y。" if self.with_guide else ""


# ── 基本投递 ──────────────────────────────────────────────

def test_appends_one_system_message_at_tail():
    messages = [("system", "既有前缀"), ("user", "问题")]
    delivered = set()

    added = append_usage_guide(_FakeRegistry(), messages, ["a", "b"], delivered=delivered)

    assert added == ["a", "b"]
    assert len(messages) == 3
    assert messages[-1][0] == "system"
    assert messages[-1][1].startswith("【已激活技能的完整使用说明】")
    assert "a 的用法" in messages[-1][1] and "b 的用法" in messages[-1][1]
    assert delivered == {"a", "b"}
    # 既有两条消息原样保留在前面
    assert messages[0] == ("system", "既有前缀")
    assert messages[1] == ("user", "问题")


def test_idempotent_skips_already_delivered():
    messages = []
    delivered = set()
    append_usage_guide(_FakeRegistry(), messages, ["a"], delivered=delivered)
    first_len = len(messages)

    added = append_usage_guide(_FakeRegistry(), messages, ["a", "b"], delivered=delivered)

    assert added == ["b"], "已投递的技能不应重复投递"
    assert len(messages) == first_len + 1


def test_no_new_skills_is_noop():
    messages = [("user", "x")]
    delivered = {"a"}
    assert append_usage_guide(_FakeRegistry(), messages, ["a"], delivered=delivered) == []
    assert len(messages) == 1


def test_registry_without_guide_marks_delivered():
    """没有可投递内容时也要记入 delivered，否则每轮都会重试同一条。"""
    messages = []
    delivered = set()
    added = append_usage_guide(_FakeRegistry(with_guide=False), messages, ["a"], delivered=delivered)
    assert added == []
    assert delivered == {"a"}
    assert messages == [], "无内容时不应产生空消息"


def test_registry_raising_is_swallowed():
    class _Boom:
        def get_skill_usage_guide(self, name):
            raise RuntimeError("boom")

    messages = []
    delivered = set()
    # build_guide_text 内部吞异常；不抛给 agent 主流程
    append_usage_guide(_Boom(), messages, ["a"], delivered=delivered)
    assert isinstance(messages, list)


# ── 前缀安全（核心约束）───────────────────────────────────

def test_never_mutates_existing_messages():
    messages = [("system", "P0"), ("user", "U1"), ("assistant", "A1")]
    snapshot = list(messages)
    append_usage_guide(_FakeRegistry(), messages, ["a", "b"])
    assert messages[:3] == snapshot, "已有消息被改动 —— 会破坏缓存前缀"


def test_never_inserts_in_middle():
    messages = [("system", "P0"), ("user", "U1")]
    append_usage_guide(_FakeRegistry(), messages, ["a"])
    assert messages.index(("system", "P0")) == 0
    assert messages[-1][1].startswith("【已激活技能的完整使用说明】")


def test_empty_registry_still_appends_marker():
    """delivered 与实际追加分离时，仍应保证只追加一条。"""
    messages = []
    append_usage_guide(_FakeRegistry(), messages, [])
    assert messages == []


# ── build_guide_text ──────────────────────────────────────

def test_build_guide_text_is_pure():
    messages = ["untouched"]
    delivered = set()
    text = build_guide_text(_FakeRegistry(), ["a"], delivered=delivered)
    assert "a 的用法" in text
    assert messages == ["untouched"], "build_guide_text 不得有副作用"
    assert delivered == set(), "build_guide_text 不得自行更新 delivered"


def test_soft_limit_hint_appears_beyond_threshold():
    delivered = {f"s{i}" for i in range(SOFT_LIMIT_SKILLS)}
    text = build_guide_text(_FakeRegistry(), ["brand_new"], delivered=delivered)
    assert "注意" in text and "只使用与当前目标相关的工具" in text


def test_soft_limit_absent_below_threshold():
    text = build_guide_text(_FakeRegistry(), ["a"], delivered=set())
    assert "注意" not in text


def test_soft_limit_is_not_a_block():
    """超过阈值只是提醒，不阻止投递。"""
    delivered = {f"s{i}" for i in range(SOFT_LIMIT_SKILLS + 5)}
    text = build_guide_text(_FakeRegistry(), ["new_skill"], delivered=delivered)
    assert "new_skill 的用法" in text

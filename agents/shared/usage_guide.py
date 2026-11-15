"""技能 usage_guide 的按需投递（PRD-A9 §2）

**为什么需要这个文件**

tools 数组是 prompt 前缀的**位置 0**，缓存只对「从头开始的连续前缀」生效。
现状（``TOOL_SURFACE_MODE=strict``）里 ``activate_skills`` 对共享 ``tools_ref``
做 ``clear() + extend()``，每轮换批都让位置 0 变化 —— 同一次 run 内后面的所有
调用 100% miss。

改成全量常驻后，「切换技能」不再需要换工具，只需要把该技能的**业务口径**
（数据源回落、口径边界、返回结构）告诉模型。而口径文本对当前请求而言是
**新鲜的**，放进 messages **尾部**即可：不影响已建立的前缀，又能让模型看到。

**前缀安全约束**（硬性）

只允许尾部追加。任何中间插入或改写都会破坏已建立的缓存，因此
:func:`append_usage_guide` 内部断言追加位置与条数，违反即抛。
"""
from __future__ import annotations

from typing import Sequence

# 投递过多时的软提示阈值（不阻止，只提醒）
SOFT_LIMIT_SKILLS = 5


def build_guide_text(registry, skill_names: Sequence[str], *,
                     delivered: Sequence[str] = ()) -> str:
    """把指定技能的 usage_guide 拼成一条 system 消息文本（不修改任何状态）。"""
    names = [s for s in (skill_names or []) if s]
    if not names:
        return ""
    delivered = set(delivered or ())
    fresh = [s for s in names if s not in delivered]
    if not fresh:
        return ""

    parts = []
    for name in fresh:
        try:
            guide = registry.get_skill_usage_guide(name)
        except Exception:
            guide = ""
        if not guide:
            continue
        parts.append(guide)

    if not parts:
        return ""

    head = "【已激活技能的完整使用说明】以下是当前激活技能的用法与口径，调用前请先读。"
    if len(delivered | set(fresh)) > SOFT_LIMIT_SKILLS:
        head += (f"\n注意：当前已加载 {len(delivered | set(fresh))} 个技能的完整说明，"
                 f"只使用与当前目标相关的工具。")
    return head + "\n\n" + "\n\n".join(parts)


def append_usage_guide(registry, messages: list, skill_names: Sequence[str],
                       *, delivered: set[str] | None = None) -> list[str]:
    """把技能 usage_guide 以 ``("system", ...)`` 追加到 messages **尾部**。

    参数
    ----
    registry    : SkillRegistry，需要 get_skill_usage_guide
    messages    : 原地追加的消息列表（必须是 list）
    skill_names : 要投递的技能名
    delivered   : 已投递集合，**原地**更新，实现幂等（已投递的跳过）

    返回
    ----
    实际新投递的技能名列表。

    ⚠️ 本函数只做尾部追加。``delivered`` 由调用方持有（推荐放在
    ``ExecutionState``，与 ``react_context_summaries`` 同处），以便 checkpoint
    恢复时统一清空——宁可重复投递也不漏。
    """
    if delivered is None:
        delivered = set()

    fresh = [s for s in (skill_names or []) if s and s not in delivered]
    if not fresh:
        return []

    text = build_guide_text(registry, fresh, delivered=delivered)
    if not text:
        # 没有可投递内容时也记为已投递，避免每轮重试同一条
        delivered.update(fresh)
        return []

    before_len = len(messages)
    before_tail = messages[-1] if messages else None

    messages.append(("system", text))

    # ── 前缀安全断言：只许尾部追加 ──────────────────────────────────
    assert len(messages) == before_len + 1, (
        f"append_usage_guide 只允许尾部追加一条，实际 {before_len} -> {len(messages)}")
    assert messages[-1] == ("system", text), "追加内容未落在 messages 尾部"
    assert (messages[-2] if before_len else None) == before_tail, (
        "追加操作改动了 messages 中已有位置，会破坏已建立的缓存前缀")

    delivered.update(fresh)
    return fresh

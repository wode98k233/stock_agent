"""运行级「当前激活技能」上下文通道（agents 写入，tools 消费）
======================================================================

**为什么需要它**：问财 SkillHub 的配额口径是「每个 skill 每天 100 次」，而记账媒介
只可能是 ``X-Claw-Skill-Id`` —— 请求端点、请求体、其余 header 完全相同，没有第二个
可区分的维度。但工具收敛为 2 个统一入口（``iwc_query`` / ``iwc_search``）后，
**工具名不再等于技能名**，底层请求层无法从工具名反推该报哪个 skill id，
否则 N 个技能的额度会被挤进同一个桶。

**写入点（覆盖全部调用路径）**：
- ``agents/shared/tool_utils.py::activate_skill``：渐进披露唯一入口，LangChain 工具路径；
  语义是「设置当前激活技能」，持续到下一次激活；
- ``tools/skill_register.py::execute_tool``：市场面板 / flow.py 等直调路径；
  语义是「本次调用属于某技能」，调用结束即恢复（用 token）。

**读取点**：``tools/other_skills/10jqka/skills.py`` 的 core 函数——仅当该技能名确实
属于问财技能集合时才采用，避免把 ``mx_data`` 这类名字当成问财 skill id 发出去。

**线程/协程安全**：ContextVar 按请求 task 隔离，写入不跨请求泄漏；
所有公开函数不抛异常（调用方无需再包 try/except）。
"""

import contextvars

# 存「当前激活技能名」（下划线形式，与 SkillMeta.skill_name 一致）
_active_skill: contextvars.ContextVar = contextvars.ContextVar(
    "active_skill_name", default=None
)


def note_active_skill(skill_name):
    """设置当前激活技能；传空值即清空。返回 token（供 restore 用，可能为 None）。

    语义偏「状态」：设置后一直有效，直到被下一次设置覆盖。
    """
    try:
        value = str(skill_name).strip() if skill_name else None
        return _active_skill.set(value or None)
    except Exception:
        return None


def restore_active_skill(token) -> None:
    """按 token 恢复上一次的值（配合 note_active_skill 做「临时覆盖」）。"""
    try:
        if token is not None:
            _active_skill.reset(token)
    except Exception:
        pass


def get_active_skill():
    """取当前激活技能名；无则 None。"""
    try:
        return _active_skill.get()
    except Exception:
        return None


def reset_active_skill() -> None:
    """清空当前上下文（测试/长驻复用场景用；请求 task 结束自动隔离，一般无需调用）。"""
    try:
        _active_skill.set(None)
    except Exception:
        pass

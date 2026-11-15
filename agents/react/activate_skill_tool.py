"""``activate_skill`` 元操作工具（PRD-A9 §2.3.3，``TOOL_SURFACE_MODE=model``）

全量常驻形态下，所有工具对模型始终可见 —— tools 数组是 prompt 缓存前缀的位置 0，
换绑即作废。代价是工具多了，模型未必知道每个技能的口径。

本工具补上这一环：模型自己决定「这个子目标该用哪个技能」，调一次本工具即拿到
该技能的完整 usage_guide（数据源回落、口径边界、返回结构），随后直接调真工具。

**为什么是「按需投递」而不是「全量前置」**：口径文本对当前请求是新鲜的，放进
messages **尾部**即可生效，且不会破坏已建立的缓存前缀。
"""
from __future__ import annotations

TOOL_NAME = "activate_skill"

_DESCRIPTION = """加载指定技能的完整使用说明（数据源、口径边界、返回结构），随后即可调用该技能下的工具。"""


def build_activate_skill_tool(registry, logger=None):
    """构造绑定到 registry 的 activate_skill LangChain 工具。

    做成闭包而不是独立技能：技能实例只拿到 (logger, memory_mgr)，拿不到
    SkillRegistry，而本工具必须反查全量技能目录。
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class ActivateSkillArgs(BaseModel):
        skill_name: str = Field(
            description="要激活的技能名，取自系统提示里的「可用技能目录」。",
        )

    def _activate(skill_name: str) -> str:
        name = (skill_name or "").strip()
        if not name:
            return "技能名为空。可用技能见 system prompt 的「## 可用技能目录」。"

        known = []
        try:
            getter = getattr(registry, "get_skill_catalog", None)
            if callable(getter):
                known = [str(i.get("skill_name") or "") for i in getter()]
        except Exception:
            known = []
        if known and name not in known:
            # 明确文案而非异常：抛异常会让模型反复重试同一个错名字
            return (f"技能「{name}」不存在。可用技能见 system prompt 的"
                    f"「## 可用技能目录」。")

        try:
            guide = registry.get_skill_usage_guide(name)
        except Exception as e:
            return f"技能「{name}」的说明加载失败：{e}"

        if not guide:
            return (f"技能「{name}」没有额外的使用说明，可直接调用其下的工具。"
                    f"若不确定有哪些工具，请重新查看「## 可用技能目录」。")

        tool_names = []
        try:
            for t in (registry.get_skill_tools(name) or []):
                # get_skill_tools 返回 dict（{"tool_name": ...}），但不同注册源
                # 也可能是带 .name/.tool_name 的对象，两种都要兼容
                if isinstance(t, dict):
                    n = t.get("tool_name") or t.get("name")
                else:
                    n = getattr(t, "name", None) or getattr(t, "tool_name", None)
                if n:
                    tool_names.append(n)
        except Exception:
            pass

        lines = [f"已激活技能「{name}」。"]
        if tool_names:
            lines.append(f"现在可以调用以下工具：{'、'.join(tool_names)}")
        lines.append("以下是它的完整使用说明：")
        lines.append(guide)
        return "\n".join(lines)

    return StructuredTool.from_function(
        func=_activate,
        name=TOOL_NAME,
        description=_DESCRIPTION,
        args_schema=ActivateSkillArgs,
    )

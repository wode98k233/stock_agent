"""
选股雷达 - 技能注册表（重构版：基于 SkillRegister）
Planner 直接选择技能名，Executor 按名加载工具
支持自动扫描技能目录，无需硬编码，新增技能无需修改本文件
每个技能为独立目录，符合 Trae Skill 标准格式
同时支持自动发现 other_skills/ 下的所有外部技能包
每个外部技能包只需提供约定的标准接口即可自动加载

约定的外部技能包接口：
- get_skill_catalog() -> str: 返回技能目录文本
- get_skill_loaders() -> dict: 返回技能加载器字典 {skill_name: build_func}
- build_all_tools(logger, memory_mgr) -> list: 构建所有技能的工具
"""
import logging
import os
from typing import TYPE_CHECKING, Any

from tools.skill_register import SkillRegister

if TYPE_CHECKING:
    from utils.memory import MemoryManager

logger = logging.getLogger("radar.skills")

# 目录层的平台分组顺序与显示名（分层渲染：平台 → 技能）
_PLATFORM_ORDER = ("internal", "eastmoney", "10jqka", "external")
_PLATFORM_LABELS = {
    "internal": "内部技能",
    "eastmoney": "东财妙想（eastmoney）",
    "10jqka": "同花顺问财（10jqka）",
    "external": "其他外部技能",
}
# 组内完全一致时可上提到平台头的字段（避免每个技能重复同一行）
_HOISTABLE_FIELDS = ("category", "key_params", "tool_names")


class SkillPromptBuilder:
    """技能提示词构建器：构建供 LLM 使用的技能目录和工具详情提示词"""
    
    @staticmethod
    def _field_text(item: dict, field: str) -> str:
        """把目录项字段规范化为可比较的字符串。"""
        if field == "category":
            return str(item.get("category") or "").strip()
        if field == "key_params":
            return "、".join(item.get("key_params") or [])
        if field == "tool_names":
            return ", ".join(item.get("tool_names") or [])
        return str(item.get(field) or "").strip()

    @staticmethod
    def _load_platform_shared(metas) -> str:
        """读取平台级共享说明（平台根目录下的 PLATFORM.md）；无则空串。

        分层原则（2026-09-21）：同一平台内**逐字重复**的说明（如同花顺的
        「注意事项」原先复制在 16 个技能包里）上提到平台层，在目录中**只渲染一次**；
        技能条目本身保留，以便按 skill id 区分限制参数、并避免描述覆盖多域导致注意力分散。
        """
        try:
            for m in metas:
                d = (getattr(m, "skill_dir", "") or "").strip()
                if not d:
                    continue
                path = os.path.join(os.path.dirname(d), "PLATFORM.md")
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read().strip()
        except Exception as e:
            logger.warning(f"读取平台共享说明失败: {e}")
        return ""

    @staticmethod
    def build_catalog_prompt(registry) -> str:
        """构建结构化技能目录提示词（目录层，按平台分层渲染）。

        结构::

            ## 可用技能目录

            ### 内部技能
            #### stock_analysis
            - 描述：...
            - 核心目标：...

            ### 同花顺问财（10jqka）
            #### 平台共享注意事项      <- PLATFORM.md，只渲染一次
            - ...
            #### 共享工具（非技能名）    <- 组内一致字段上提；工具名不是技能名
            - 分类：...
            #### hithink_market_query
            - 描述：...
        """
        catalog = registry.get_skill_catalog()

        # 1) 按平台分组（保持目录原有顺序）
        groups: dict = {}
        for item in catalog:
            meta = None
            try:
                meta = registry.get_skill_meta(item["skill_name"])
            except Exception:
                meta = None
            plat = getattr(meta, "platform", "internal") if meta else "internal"
            groups.setdefault(plat, []).append((item, meta))

        lines = ["## 可用技能目录", ""]
        ordered = [p for p in _PLATFORM_ORDER if p in groups]
        ordered += [p for p in groups if p not in _PLATFORM_ORDER]

        for plat in ordered:
            entries = groups[plat]
            lines.append(f"### {_PLATFORM_LABELS.get(plat, plat)}")

            # 平台级共享说明（PLATFORM.md）：只渲染一次
            shared = SkillPromptBuilder._load_platform_shared([m for _, m in entries if m])
            if shared:
                lines.append(shared)
            lines.append("")

            # 组内「工具相同」的子组：把一致的字段上提到子组头，只写一次
            # （同平台内可能有多套工具，如 iwc_query / iwc_search，各自参数不同）
            sub_hoisted = {}
            for key in {SkillPromptBuilder._field_text(it, "tool_names") for it, _ in entries}:
                group = [(it, m) for it, m in entries
                         if SkillPromptBuilder._field_text(it, "tool_names") == key]
                h = {}
                for field in _HOISTABLE_FIELDS:
                    vals = {SkillPromptBuilder._field_text(it, field) for it, _ in group}
                    if len(vals) == 1 and next(iter(vals)):
                        h[field] = next(iter(vals))
                sub_hoisted[key] = h
            emitted_subgroups = set()

            for item, _meta in entries:
                key = SkillPromptBuilder._field_text(item, "tool_names")
                h = sub_hoisted.get(key, {})
                if key not in emitted_subgroups:
                    emitted_subgroups.add(key)
                    if h:
                        lines.append(f"#### 共享工具（非技能名）：{key or '无'}")
                        if "category" in h:
                            lines.append(f"- 分类：{h['category']}")
                        if "key_params" in h:
                            lines.append(f"- 关键参数：{h['key_params']}")
                        lines.append("")

                lines.append(f"#### {item['skill_name']}")
                lines.append(f"- 描述：{item['description']}")
                if "category" not in h:
                    lines.append(f"- 分类：{item['category']}")
                if "key_params" not in h:
                    kp = "、".join(item["key_params"]) if item["key_params"] else "无"
                    lines.append(f"- 关键参数：{kp}")
                lines.append(f"- 核心目标：{item['target']}")
                if "tool_names" not in h:
                    lines.append(f"- 可用工具：{item['tool_names'] and ', '.join(item['tool_names']) or '无'}")
                lines.append("")
        return "\n".join(lines)
    
    @staticmethod
    def build_tools_detail_prompt(registry, skill_name: str) -> str:
        """构建工具参数约束提示词（工具层），包含使用指南"""
        tools = registry.get_skill_tools(skill_name)
        usage_guide = registry.get_skill_usage_guide(skill_name)
        if not tools and not usage_guide:
            return ""
        lines = [f"## 技能 {skill_name} 的工具详情", ""]
        if usage_guide:
            lines.append("### 使用指南")
            lines.append(usage_guide)
            lines.append("")
        for t in tools:
            lines.append(f"### {t['tool_name']}")
            lines.append(f"- 功能：{t['description']}")
            schema = t.get('param_schema', {})
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if properties:
                lines.append("- 参数:")
                for pname, pinfo in properties.items():
                    req_mark = "必填" if pname in required else "可选"
                    desc = pinfo.get("description", "")
                    ptype = pinfo.get("type", "any")
                    default = pinfo.get("default")
                    default_str = f", 默认={default}" if default is not None else ""
                    lines.append(f"  - {pname}: {ptype}（{req_mark}{default_str}）- {desc}")
            lines.append("")
        return "\n".join(lines)


class SkillRegistry:
    def __init__(self, logger, memory_mgr: Any, register: SkillRegister):
        self.logger = logger
        self.memory_mgr = memory_mgr
        self._register = register
        self._cache = {}

    def get_catalog_text(self) -> str:
        return self._register.get_catalog_text()

    def get_tools(self, skill_name: str) -> list:
        if skill_name not in self._cache:
            tools = self._register.build_langchain_tools(
                skill_name, self.logger, self.memory_mgr
            )
            if not tools:
                self.logger.warning(f"技能 '{skill_name}' 无对应工具")
            self._cache[skill_name] = tools
        return self._cache[skill_name]

    def get_all_tools(self) -> list:
        tools = []
        seen = set()
        for name in self._register.get_all_skills().keys():
            for t in self.get_tools(name):
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        return tools

    def get_skill_catalog(self) -> list:
        return self._register.get_skill_catalog()

    def get_skill_tools(self, skill_name: str) -> list:
        return self._register.get_skill_tools(skill_name)

    def get_skill_meta(self, skill_name: str):
        return self._register.get_skill(skill_name)

    def get_skill_usage_guide(self, skill_name: str) -> str:
        return self._register.get_skill_usage_guide(skill_name)

    def execute_tool(self, skill_name: str, tool_name: str, params: dict):
        return self._register.execute_tool(skill_name, tool_name, params)

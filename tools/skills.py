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
from typing import TYPE_CHECKING, Any

from tools.skill_register import SkillRegister

if TYPE_CHECKING:
    from utils.memory import MemoryManager

logger = logging.getLogger("radar.skills")


class SkillPromptBuilder:
    """技能提示词构建器：构建供 LLM 使用的技能目录和工具详情提示词"""
    
    @staticmethod
    def build_catalog_prompt(registry) -> str:
        """构建结构化技能目录提示词（目录层）"""
        catalog = registry.get_skill_catalog()
        lines = ["## 可用技能目录", ""]
        for item in catalog:
            lines.append(f"### {item['skill_name']}")
            lines.append(f"- 描述：{item['description']}")
            lines.append(f"- 分类：{item['category']}")
            lines.append(f"- 关键参数：{', '.join(item['key_params']) if item['key_params'] else '无'}")
            lines.append(f"- 核心目标：{item['target']}")
            lines.append(f"- 可用工具：{', '.join(item['tool_names'])}")
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

"""
选股雷达 - Skill 注册器核心模块
提供 SkillMeta/ToolMeta 数据结构、自动发现与注册、目录/工具列表提取

核心设计:
  1. SkillMeta/ToolMeta 标准化元数据，与 SKILL.md 一一对应
  2. SkillRegister 自动发现内部/外部技能，递归解析子技能
  3. 提供目录层(get_skill_catalog)和工具层(get_skill_tools)接口，供 Agent 分层调用
  4. 兼容现有 build_tools() 接口和 TOOL_REGISTRY 注册规范
"""
import os
import sys
import importlib.util
import logging
import yaml
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Type, Callable, Tuple

from pydantic import BaseModel

logger = logging.getLogger("radar.skill_register")

from utils.app_paths import get_internal_skills_dir, get_external_skills_dir

INTERNAL_SKILLS_ROOT = get_internal_skills_dir()
EXTERNAL_SKILLS_ROOT = get_external_skills_dir()


@dataclass
class ToolParamMeta:
    """工具参数元数据"""
    name: str
    type_str: str
    required: bool
    description: str = ""
    default: Any = None


@dataclass
class ToolMeta:
    """工具元数据（对应 SKILL.md 工具列表）"""
    tool_name: str
    description: str
    param_model: Optional[Type[BaseModel]] = None
    tool_func: Optional[Callable] = None
    params: List[ToolParamMeta] = field(default_factory=list)

    def get_param_schema(self) -> Dict[str, Any]:
        if self.param_model:
            return self.param_model.model_json_schema()
        schema = {}
        for p in self.params:
            schema[p.name] = {
                "type": p.type_str,
                "required": p.required,
                "description": p.description,
            }
            if p.default is not None:
                schema[p.name]["default"] = p.default
        return schema


@dataclass
class SkillMeta:
    """Skill 元数据（核心注册单元）"""
    skill_name: str
    skill_version: str = "v1.0"
    skill_desc: str = ""
    category: str = ""
    catalog_info: Dict[str, Any] = field(default_factory=lambda: {
        "key_params": [],
        "target": ""
    })
    tools: List[ToolMeta] = field(default_factory=list)
    sub_skills: List["SkillMeta"] = field(default_factory=list)
    related_files: List[str] = field(default_factory=list)
    skill_dir: str = ""
    build_tools_func: Optional[Callable] = None
    tool_registry: Optional[Dict[str, Tuple[Callable, Type[BaseModel]]]] = None
    usage_guide: str = ""


def _parse_skill_md(md_path: str) -> Tuple[Dict[str, Any], str]:
    """解析 SKILL.md 文件，返回 (yaml_meta, markdown_content)"""
    with open(md_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    if md_content.startswith('---'):
        parts = md_content.split('---', 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1].strip()) or {}
            except yaml.YAMLError:
                meta = {}
            content = parts[2].strip()
            return meta, content
    return {}, md_content


def _parse_catalog_info(content: str) -> Dict[str, Any]:
    """从 SKILL.md 内容中解析目录层信息"""
    catalog_info = {"key_params": [], "target": ""}
    lines = content.split('\n')
    in_catalog = False
    for line in lines:
        if re.match(r'^##\s*目录层信息', line):
            in_catalog = True
            continue
        if in_catalog and line.startswith('## '):
            break
        if in_catalog:
            param_match = re.match(r'^-\s*关键参数[：:]\s*(.+)', line)
            if param_match:
                params_str = param_match.group(1)
                catalog_info["key_params"] = [
                    p.strip() for p in params_str.split('、') if p.strip()
                ]
            target_match = re.match(r'^-\s*核心目标[：:]\s*(.+)', line)
            if target_match:
                catalog_info["target"] = target_match.group(1).strip()
    return catalog_info


def _parse_tool_list(content: str) -> List[Dict[str, Any]]:
    """从 SKILL.md 内容中解析工具列表"""
    tools = []
    lines = content.split('\n')
    current_tool = None
    in_tools = False

    for line in lines:
        if re.match(r'^##\s*工具列表', line):
            in_tools = True
            continue
        if in_tools and line.startswith('## ') and not re.match(r'^##\s*工具列表', line):
            break
        if not in_tools:
            continue

        tool_header = re.match(r'^###\s*工具\d+[：:]\s*(.+)', line)
        if tool_header:
            if current_tool:
                tools.append(current_tool)
            current_tool = {"tool_name": tool_header.group(1).strip(), "description": "", "params": []}
            continue

        if current_tool is None:
            continue

        desc_match = re.match(r'^-\s*功能[：:]\s*(.+)', line)
        if desc_match:
            current_tool["description"] = desc_match.group(1).strip()
            continue

        entry_match = re.match(r'^-\s*调用入口[：:]\s*(.+)', line)
        if entry_match:
            current_tool["entry"] = entry_match.group(1).strip()
            continue

        param_match = re.match(r'^\s+-\s*(\w+)\s*:\s*(\w+)[（(](必填|可选)[）)]\s*-\s*(.+)', line)
        if param_match:
            current_tool["params"].append({
                "name": param_match.group(1),
                "type": param_match.group(2),
                "required": param_match.group(3) == "必填",
                "description": param_match.group(4).strip()
            })
            continue

        param_match2 = re.match(r'^\s+-\s*(\w+)\s*:\s*(\w+)\s*(?:，|,)\s*可选\s*[，,]?\s*默认(\S+)\s*-\s*(.+)', line)
        if param_match2:
            current_tool["params"].append({
                "name": param_match2.group(1),
                "type": param_match2.group(2),
                "required": False,
                "default": param_match2.group(3),
                "description": param_match2.group(4).strip()
            })

    if current_tool:
        tools.append(current_tool)
    return tools


def _parse_usage_guide(content: str) -> str:
    """从 SKILL.md 内容中解析使用指南（供 LLM 理解如何调用工具）"""
    lines = content.split('\n')
    in_guide = False
    guide_lines = []
    for line in lines:
        if re.match(r'^##\s*使用指南', line):
            in_guide = True
            continue
        if in_guide and line.startswith('## '):
            break
        if in_guide:
            guide_lines.append(line)
    return "\n".join(guide_lines).strip()


def _build_tool_meta_from_md(tool_info: Dict[str, Any]) -> ToolMeta:
    """从 SKILL.md 解析的工具信息构建 ToolMeta"""
    params = []
    for p in tool_info.get("params", []):
        params.append(ToolParamMeta(
            name=p["name"],
            type_str=p.get("type", "str"),
            required=p.get("required", True),
            description=p.get("description", ""),
            default=p.get("default"),
        ))
    return ToolMeta(
        tool_name=tool_info.get("tool_name", ""),
        description=tool_info.get("description", ""),
        params=params,
    )


def _load_module_from_file(file_path: str, module_name: str):
    """动态加载 Python 模块"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        logger.error(f"模块规范加载失败: {module_name} (路径:{file_path})")
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        # 补充详细异常日志
        logger.error(f"加载外部模块失败 {module_name} (路径:{file_path}): {str(e)}", exc_info=True)
        return None


class SkillRegister:
    """Skill 注册器：自动发现、注册、目录/工具提取"""

    def __init__(self):
        self._registry: Dict[str, SkillMeta] = {}
        self._initialized = False

    def auto_discover(self, skill_dirs: Optional[List[str]] = None) -> None:
        if self._initialized:
            return

        if skill_dirs is None:
            skill_dirs = [INTERNAL_SKILLS_ROOT, EXTERNAL_SKILLS_ROOT]

        for dir_path in skill_dirs:
            if not os.path.exists(dir_path):
                continue
            if dir_path == INTERNAL_SKILLS_ROOT:
                self._discover_internal_skills(dir_path)
            elif dir_path == EXTERNAL_SKILLS_ROOT:
                self._discover_external_skills(dir_path)
            elif os.path.basename(dir_path) == "other_skills":
                self._discover_external_skills(dir_path)
            else:
                self._discover_internal_skills(dir_path)

        self._initialized = True
        logger.info(f"SkillRegister 初始化完成，共注册 {len(self._registry)} 个技能: "
                     f"{list(self._registry.keys())}")

    def _discover_internal_skills(self, skills_root: str) -> None:
        if not os.path.exists(skills_root):
            return
        for item in os.listdir(skills_root):
            item_path = os.path.join(skills_root, item)
            if not os.path.isdir(item_path) or item.startswith('.') or item == '__pycache__':
                continue
            skill_meta = self._load_internal_skill(item_path)
            if skill_meta:
                self.register_skill(skill_meta)

    def _load_internal_skill(self, skill_dir: str) -> Optional[SkillMeta]:
        skill_name = os.path.basename(skill_dir)
        main_path = os.path.join(skill_dir, "main.py")
        skill_md_path = os.path.join(skill_dir, "SKILL.md")

        if not os.path.exists(skill_md_path):
            return None

        meta, content = _parse_skill_md(skill_md_path)
        actual_name = meta.get("name", skill_name)
        catalog_info = _parse_catalog_info(content)
        tool_list = _parse_tool_list(content)
        usage_guide = _parse_usage_guide(content)

        tool_metas = [_build_tool_meta_from_md(t) for t in tool_list]

        related_files = []
        if os.path.exists(main_path):
            related_files.append(main_path)

        skill_meta = SkillMeta(
            skill_name=actual_name,
            skill_version=meta.get("version", "v1.0"),
            skill_desc=meta.get("description", ""),
            category=meta.get("category", ""),
            catalog_info=catalog_info,
            tools=tool_metas,
            related_files=related_files,
            skill_dir=skill_dir,
            usage_guide=usage_guide,
        )

        if os.path.exists(main_path):
            module = _load_module_from_file(main_path, f"tools.skills.{skill_name}")
            if module:
                if hasattr(module, 'build_tools'):
                    skill_meta.build_tools_func = module.build_tools
                if hasattr(module, 'TOOL_REGISTRY'):
                    skill_meta.tool_registry = module.TOOL_REGISTRY
                    self._enrich_tool_metas_from_registry(skill_meta)

        return skill_meta

    def _enrich_tool_metas_from_registry(self, skill_meta: SkillMeta) -> None:
        if not skill_meta.tool_registry:
            return
        for tool_name, (tool_func, param_model) in skill_meta.tool_registry.items():
            for tm in skill_meta.tools:
                if tm.tool_name == tool_name:
                    tm.tool_func = tool_func
                    tm.param_model = param_model
                    break
            else:
                schema = param_model.model_json_schema() if param_model else {}
                properties = schema.get("properties", {})
                required_fields = schema.get("required", [])
                params = []
                for pname, pinfo in properties.items():
                    params.append(ToolParamMeta(
                        name=pname,
                        type_str=pinfo.get("type", "str"),
                        required=pname in required_fields,
                        description=pinfo.get("description", ""),
                        default=pinfo.get("default"),
                    ))
                desc = ""
                if tool_func and tool_func.__doc__:
                    desc = tool_func.__doc__.strip().split('\n')[0]
                skill_meta.tools.append(ToolMeta(
                    tool_name=tool_name,
                    description=desc,
                    param_model=param_model,
                    tool_func=tool_func,
                    params=params,
                ))

    def _discover_external_skills(self, skills_root: str) -> None:
        if not os.path.exists(skills_root):
            return
        for item in os.listdir(skills_root):
            item_path = os.path.join(skills_root, item)
            if not os.path.isdir(item_path) or item.startswith('.') or item == '__pycache__':
                continue
            self._load_external_skill_package(item_path)

    def _load_external_skill_package(self, package_dir: str) -> None:
        package_name = os.path.basename(package_dir)
        skills_py_path = os.path.join(package_dir, "skills.py")

        if not os.path.exists(skills_py_path):
            self._discover_external_sub_skills(package_dir)
            return

        import tools.other_skills
        import importlib
        try:
            pkg = importlib.import_module(f"tools.other_skills.{package_name}")
        except ImportError:
            pkg = None

        loader_module_name = f"tools.other_skills.{package_name}.loader"
        module = _load_module_from_file(skills_py_path, loader_module_name)
        if not module:
            return

        if hasattr(module, 'get_skill_catalog') and hasattr(module, 'get_skill_loaders'):
            loaders = module.get_skill_loaders()
            catalog_text = module.get_skill_catalog()

            sub_skill_metas = {}
            sub_skill_dirs = [
                d for d in os.listdir(package_dir)
                if os.path.isdir(os.path.join(package_dir, d))
                and not d.startswith('.') and d != '__pycache__'
                and os.path.exists(os.path.join(package_dir, d, "SKILL.md"))
            ]
            for sub_dir_name in sub_skill_dirs:
                sub_dir = os.path.join(package_dir, sub_dir_name)
                sub_meta = self._load_external_sub_skill(sub_dir, parent_name="")
                if sub_meta:
                    sub_skill_metas[sub_meta.skill_name] = sub_meta

            for skill_name, loader in loaders.items():
                existing_meta = sub_skill_metas.get(skill_name)
                if existing_meta:
                    existing_meta.build_tools_func = loader
                    if hasattr(module, 'TOOL_REGISTRY'):
                        existing_meta.tool_registry = module.TOOL_REGISTRY.copy()
                        if existing_meta.tool_registry:
                            self._enrich_tool_metas_from_registry(existing_meta)
                    self.register_skill(existing_meta)
                else:
                    skill_meta = SkillMeta(
                        skill_name=skill_name,
                        skill_desc=_extract_first_line_from_catalog(catalog_text, skill_name),
                        build_tools_func=loader,
                        skill_dir=package_dir,
                    )
                    if hasattr(module, 'TOOL_REGISTRY'):
                        skill_meta.tool_registry = module.TOOL_REGISTRY.copy()
                    self.register_skill(skill_meta)

    def _discover_external_sub_skills(self, package_dir: str) -> None:
        for item in os.listdir(package_dir):
            item_path = os.path.join(package_dir, item)
            if not os.path.isdir(item_path) or item.startswith('.') or item == '__pycache__':
                continue
            sub_meta = self._load_external_sub_skill(item_path)
            if sub_meta:
                self.register_skill(sub_meta)

    def _load_external_sub_skill(self, skill_dir: str, parent_name: str = "") -> Optional[SkillMeta]:
        skill_name = os.path.basename(skill_dir)
        skill_md_path = os.path.join(skill_dir, "SKILL.md")

        if not os.path.exists(skill_md_path):
            return None

        meta, content = _parse_skill_md(skill_md_path)
        actual_name = meta.get("name", skill_name).replace("-", "_")
        catalog_info = _parse_catalog_info(content)
        tool_list = _parse_tool_list(content)
        usage_guide = _parse_usage_guide(content)
        tool_metas = [_build_tool_meta_from_md(t) for t in tool_list]

        impl_file = os.path.join(skill_dir, actual_name + ".py")
        related_files = []
        if os.path.exists(impl_file):
            related_files.append(impl_file)

        skill_meta = SkillMeta(
            skill_name=actual_name,
            skill_version=str(meta.get("version", "v1.0")),
            skill_desc=meta.get("description", meta.get("display_name", "")),
            category=meta.get("category", ""),
            catalog_info=catalog_info,
            tools=tool_metas,
            related_files=related_files,
            skill_dir=skill_dir,
            usage_guide=usage_guide,
        )

        if os.path.exists(impl_file):
            module = _load_module_from_file(impl_file, f"external.{actual_name}")
            if module and hasattr(module, 'TOOL_REGISTRY'):
                skill_meta.tool_registry = module.TOOL_REGISTRY
                self._enrich_tool_metas_from_registry(skill_meta)

        return skill_meta

    def register_skill(self, skill_meta: SkillMeta) -> None:
        self._registry[skill_meta.skill_name] = skill_meta
        for sub_skill in skill_meta.sub_skills:
            self.register_skill(sub_skill)

    def get_skill(self, skill_name: str) -> Optional[SkillMeta]:
        return self._registry.get(skill_name)

    def get_all_skills(self) -> Dict[str, SkillMeta]:
        return self._registry.copy()

    def get_skill_catalog(self) -> List[Dict[str, Any]]:
        catalog = []
        for skill_meta in self._registry.values():
            catalog.append({
                "skill_name": skill_meta.skill_name,
                "description": skill_meta.skill_desc,
                "category": skill_meta.category,
                "key_params": skill_meta.catalog_info.get("key_params", []),
                "target": skill_meta.catalog_info.get("target", ""),
                "tools_count": len(skill_meta.tools),
                "tool_names": [t.tool_name for t in skill_meta.tools],
            })
        return catalog

    def get_skill_tools(self, skill_name: str) -> List[Dict[str, Any]]:
        skill_meta = self.get_skill(skill_name)
        if not skill_meta:
            return []
        tools = []
        for tool_meta in skill_meta.tools:
            tools.append({
                "tool_name": tool_meta.tool_name,
                "description": tool_meta.description,
                "param_schema": tool_meta.get_param_schema(),
            })
        return tools

    def get_skill_usage_guide(self, skill_name: str) -> str:
        skill_meta = self.get_skill(skill_name)
        if not skill_meta:
            return ""
        return skill_meta.usage_guide

    def get_catalog_text(self) -> str:
        catalog = self.get_skill_catalog()
        lines = ["## 可用技能及工具"]
        for item in catalog:
            desc = item["description"].split('，')[0] if item["description"] else ""
            lines.append(f"### {item['skill_name']}({desc})")
            for tn in item["tool_names"]:
                skill_meta = self.get_skill(item["skill_name"])
                if skill_meta:
                    for t in skill_meta.tools:
                        if t.tool_name == tn:
                            lines.append(f"- **{t.tool_name}**: {t.description}")
                            break
        return "\n".join(lines)

    def build_langchain_tools(self, skill_name: str, logger_obj, memory_mgr) -> list:
        skill_meta = self.get_skill(skill_name)
        if not skill_meta:
            return []
        if skill_meta.build_tools_func:
            try:
                return skill_meta.build_tools_func(logger_obj, memory_mgr)
            except Exception as e:
                logger.warning(f"构建技能 {skill_name} 工具失败: {e}")
                return []
        return []

    def build_all_langchain_tools(self, logger_obj, memory_mgr) -> list:
        tools = []
        seen = set()
        for name in self._registry.keys():
            for t in self.build_langchain_tools(name, logger_obj, memory_mgr):
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        return tools

    def execute_tool(self, skill_name: str, tool_name: str, params: Dict[str, Any]) -> Any:
        skill_meta = self.get_skill(skill_name)
        if not skill_meta:
            raise ValueError(f"技能 {skill_name} 未注册")

        tool_meta = None
        for t in skill_meta.tools:
            if t.tool_name == tool_name:
                tool_meta = t
                break

        if not tool_meta:
            raise ValueError(f"工具 {tool_name} 在技能 {skill_name} 中未找到")

        if tool_meta.param_model:
            validated = tool_meta.param_model(**params)
            params = validated.model_dump()

        if tool_meta.tool_func: 
            return tool_meta.tool_func(**params)

        if skill_meta.tool_registry and tool_name in skill_meta.tool_registry:
            tool_func, param_model = skill_meta.tool_registry[tool_name]
            if param_model:
                validated = param_model(**params)
                params = validated.model_dump()
            return tool_func(**params)

        raise ValueError(f"工具 {tool_name} 无可执行函数")

    def reset(self) -> None:
        self._registry = {}
        self._initialized = False


def _extract_first_line_from_catalog(catalog_text: str, skill_name: str) -> str:
    """从目录文本中提取技能描述的首行"""
    for line in catalog_text.split('\n'):
        if skill_name in line and '(' in line:
            match = re.search(r'\(([^)]+)\)', line)
            if match:
                return match.group(1)
    return ""

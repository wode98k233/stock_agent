"""
选股雷达 - Skill 规范化检查脚本
检查项:
  1. SKILL.md 完整性：是否包含模板要求的所有模块
  2. 工具注册一致性：main.py/skill.py 的 TOOL_REGISTRY 与 SKILL.md 工具列表是否一一对应
  3. 参数约束一致性：代码中 Pydantic 参数模型与 SKILL.md 参数约束是否一致
  4. build_tools 函数存在性检查

用法:
  python -m tools.skill_checker              # 检查所有技能
  python -m tools.skill_checker --fix-hints   # 显示修复建议
  python -m tools.skill_checker --skill stock_query  # 检查指定技能
"""
import os
import sys
import re
import io
import yaml
import importlib.util
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field

from utils.app_paths import get_internal_skills_dir, get_external_skills_dir

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

INTERNAL_SKILLS_ROOT = get_internal_skills_dir()
EXTERNAL_SKILLS_ROOT = get_external_skills_dir()

REQUIRED_SECTIONS = ["基础信息", "关联文件", "目录层信息", "工具列表"]
OPTIONAL_SECTIONS = []

REQUIRED_YAML_FIELDS = ["name", "version", "description"]

SKILL_MD_REQUIRED_SUBSECTIONS = {
    "基础信息": ["版本", "描述", "适用场景"],
    "关联文件": ["工具注册入口"],
    "目录层信息": ["关键参数", "核心目标"],
    "工具列表": [],
}


@dataclass
class CheckResult:
    skill_name: str
    skill_dir: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def __str__(self):
        status = "✅ PASS" if self.passed else "❌ FAIL"
        lines = [f"\n{'='*60}", f"{status} {self.skill_name} ({self.skill_dir})", f"{'='*60}"]
        if self.errors:
            lines.append("  错误:")
            for e in self.errors:
                lines.append(f"    ❌ {e}")
        if self.warnings:
            lines.append("  警告:")
            for w in self.warnings:
                lines.append(f"    ⚠️  {w}")
        if self.info:
            lines.append("  信息:")
            for i in self.info:
                lines.append(f"    ℹ️  {i}")
        return "\n".join(lines)


def _parse_skill_md(md_path: str) -> Tuple[Dict[str, Any], str]:
    from tools.skill_register import _parse_skill_md as _parse
    return _parse(md_path)


def _extract_sections(content: str) -> Dict[str, str]:
    sections = {}
    current_header = None
    current_lines = []
    for line in content.split('\n'):
        header_match = re.match(r'^##\s*(.+)', line)
        if header_match:
            if current_header:
                sections[current_header] = '\n'.join(current_lines)
            current_header = header_match.group(1).strip()
            current_lines = []
        elif current_header:
            current_lines.append(line)
    if current_header:
        sections[current_header] = '\n'.join(current_lines)
    return sections


def _extract_tool_names_from_md(content: str) -> List[str]:
    tools = []
    for match in re.finditer(r'^###\s*工具\d+[：:]\s*(.+)', content, re.MULTILINE):
        tools.append(match.group(1).strip())
    return tools


def _extract_tool_params_from_md(content: str, tool_name: str) -> List[Dict[str, str]]:
    params = []
    in_target_tool = False
    in_params = False
    lines = content.split('\n')
    for line in lines:
        if re.match(r'^###\s*工具\d+[：:]\s*' + re.escape(tool_name), line):
            in_target_tool = True
            continue
        if in_target_tool and line.startswith('### '):
            break
        if in_target_tool and '参数约束' in line:
            in_params = True
            continue
        if in_params:
            param_match = re.match(r'^\s+-\s*(\w+)\s*:\s*(\w+)[（(](必填|可选)', line)
            if param_match:
                params.append({
                    "name": param_match.group(1),
                    "type": param_match.group(2),
                    "required": param_match.group(3) == "必填",
                })
    return params


def _load_tool_registry(main_path: str, module_name: str) -> Optional[Dict]:
    if not os.path.exists(main_path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, main_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if hasattr(module, 'TOOL_REGISTRY'):
            return module.TOOL_REGISTRY
    except Exception as e:
        return None
    return None


def _get_pydantic_fields(param_model) -> Dict[str, Dict[str, Any]]:
    try:
        schema = param_model.model_json_schema()
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        fields = {}
        for name, info in properties.items():
            fields[name] = {
                "type": info.get("type", "any"),
                "required": name in required,
                "description": info.get("description", ""),
                "default": info.get("default"),
            }
        return fields
    except Exception:
        return {}


def check_skill_md_completeness(skill_dir: str, result: CheckResult) -> None:
    md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(md_path):
        result.errors.append("SKILL.md 文件不存在")
        return

    meta, content = _parse_skill_md(md_path)
    for field_name in REQUIRED_YAML_FIELDS:
        if field_name not in meta or not meta[field_name]:
            result.errors.append(f"SKILL.md YAML 元数据缺少必填字段: {field_name}")

    sections = _extract_sections(content)
    for section in REQUIRED_SECTIONS:
        if section not in sections:
            result.errors.append(f"SKILL.md 缺少必填章节: {section}")

    for section_name, required_subsections in SKILL_MD_REQUIRED_SUBSECTIONS.items():
        if section_name not in sections:
            continue
        section_content = sections[section_name]
        for sub in required_subsections:
            if sub not in section_content:
                result.warnings.append(f"SKILL.md 章节 '{section_name}' 缺少子项: {sub}")

    tool_names = _extract_tool_names_from_md(content)
    if not tool_names:
        result.warnings.append("SKILL.md 工具列表为空或未按标准格式编写")
    else:
        result.info.append(f"SKILL.md 中声明了 {len(tool_names)} 个工具: {tool_names}")


def check_tool_registry_consistency(skill_dir: str, result: CheckResult) -> None:
    md_path = os.path.join(skill_dir, "SKILL.md")
    main_path = os.path.join(skill_dir, "main.py")
    skill_name = os.path.basename(skill_dir)

    if not os.path.exists(md_path):
        return

    _, content = _parse_skill_md(md_path)
    md_tool_names = _extract_tool_names_from_md(content)

    tool_registry = _load_tool_registry(main_path, f"checker.{skill_name}")
    if tool_registry is None:
        if os.path.exists(main_path):
            result.warnings.append("main.py 存在但未定义 TOOL_REGISTRY")
        else:
            result.warnings.append("main.py 不存在")
        return

    code_tool_names = list(tool_registry.keys())
    result.info.append(f"TOOL_REGISTRY 中注册了 {len(code_tool_names)} 个工具: {code_tool_names}")

    md_set = set(md_tool_names)
    code_set = set(code_tool_names)

    only_in_md = md_set - code_set
    only_in_code = code_set - md_set

    if only_in_md:
        result.errors.append(f"SKILL.md 中有但 TOOL_REGISTRY 中未注册的工具: {only_in_md}")
    if only_in_code:
        result.errors.append(f"TOOL_REGISTRY 中有但 SKILL.md 中未声明的工具: {only_in_code}")


def check_param_consistency(skill_dir: str, result: CheckResult) -> None:
    md_path = os.path.join(skill_dir, "SKILL.md")
    main_path = os.path.join(skill_dir, "main.py")
    skill_name = os.path.basename(skill_dir)

    if not os.path.exists(md_path) or not os.path.exists(main_path):
        return

    _, content = _parse_skill_md(md_path)
    tool_registry = _load_tool_registry(main_path, f"checker.param.{skill_name}")
    if tool_registry is None:
        return

    for tool_name, (func, param_model) in tool_registry.items():
        md_params = _extract_tool_params_from_md(content, tool_name)
        code_fields = _get_pydantic_fields(param_model)

        if not md_params and not code_fields:
            continue

        md_param_names = {p["name"] for p in md_params}
        code_param_names = set(code_fields.keys())

        only_in_md = md_param_names - code_param_names
        only_in_code = code_param_names - md_param_names

        if only_in_md:
            result.warnings.append(
                f"工具 '{tool_name}': SKILL.md 中有参数但代码中未定义: {only_in_md}"
            )
        if only_in_code:
            result.warnings.append(
                f"工具 '{tool_name}': 代码中有参数但 SKILL.md 中未声明: {only_in_code}"
            )

        for md_p in md_params:
            if md_p["name"] in code_fields:
                code_p = code_fields[md_p["name"]]
                if md_p["required"] != code_p["required"]:
                    result.warnings.append(
                        f"工具 '{tool_name}' 参数 '{md_p['name']}': "
                        f"必填不一致 (SKILL.md={md_p['required']}, 代码={code_p['required']})"
                    )


def check_build_tools(skill_dir: str, result: CheckResult) -> None:
    main_path = os.path.join(skill_dir, "main.py")
    skill_name = os.path.basename(skill_dir)

    if not os.path.exists(main_path):
        result.warnings.append("main.py 不存在，无法检查 build_tools 函数")
        return

    try:
        spec = importlib.util.spec_from_file_location(f"checker.bt.{skill_name}", main_path)
        if spec is None or spec.loader is None:
            result.errors.append("main.py 无法加载")
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, 'build_tools'):
            result.errors.append("main.py 缺少 build_tools 函数")
        else:
            result.info.append("main.py 包含 build_tools 函数")
    except Exception as e:
        result.errors.append(f"main.py 加载失败: {e}")


def check_single_skill(skill_dir: str) -> CheckResult:
    skill_name = os.path.basename(skill_dir)
    result = CheckResult(skill_name=skill_name, skill_dir=skill_dir)

    check_skill_md_completeness(skill_dir, result)
    check_tool_registry_consistency(skill_dir, result)
    check_param_consistency(skill_dir, result)
    check_build_tools(skill_dir, result)

    return result


def check_all_skills() -> List[CheckResult]:
    results = []

    if os.path.exists(INTERNAL_SKILLS_ROOT):
        for item in sorted(os.listdir(INTERNAL_SKILLS_ROOT)):
            item_path = os.path.join(INTERNAL_SKILLS_ROOT, item)
            if os.path.isdir(item_path) and not item.startswith('.') and item != '__pycache__':
                results.append(check_single_skill(item_path))

    if os.path.exists(EXTERNAL_SKILLS_ROOT):
        for package_item in sorted(os.listdir(EXTERNAL_SKILLS_ROOT)):
            package_path = os.path.join(EXTERNAL_SKILLS_ROOT, package_item)
            if not os.path.isdir(package_path) or package_item.startswith('.') or package_item == '__pycache__':
                continue
            for sub_item in sorted(os.listdir(package_path)):
                sub_path = os.path.join(package_path, sub_item)
                if os.path.isdir(sub_path) and not sub_item.startswith('.') and sub_item != '__pycache__':
                    if os.path.exists(os.path.join(sub_path, "SKILL.md")):
                        results.append(check_single_skill(sub_path))

    return results


def main():
    fix_hints = "--fix-hints" in sys.argv
    target_skill = None
    for arg in sys.argv[1:]:
        if arg.startswith("--skill="):
            target_skill = arg.split("=", 1)[1]
        elif arg == "--skill" and sys.argv.index(arg) + 1 < len(sys.argv):
            target_skill = sys.argv[sys.argv.index(arg) + 1]

    if target_skill:
        skill_dir = os.path.join(INTERNAL_SKILLS_ROOT, target_skill)
        if not os.path.exists(skill_dir):
            skill_dir = None
            if os.path.exists(EXTERNAL_SKILLS_ROOT):
                for pkg in os.listdir(EXTERNAL_SKILLS_ROOT):
                    candidate = os.path.join(EXTERNAL_SKILLS_ROOT, pkg, target_skill)
                    if os.path.exists(candidate):
                        skill_dir = candidate
                        break
        if skill_dir and os.path.exists(skill_dir):
            result = check_single_skill(skill_dir)
            print(result)
        else:
            print(f"❌ 技能 '{target_skill}' 未找到")
    else:
        results = check_all_skills()
        for r in results:
            print(r)

        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        print(f"\n{'='*60}")
        print(f"检查完成: 共 {total} 个技能, ✅ {passed} 通过, ❌ {failed} 失败")
        if failed > 0:
            print(f"失败技能: {[r.skill_name for r in results if not r.passed]}")
            sys.exit(1)
        else:
            print("所有技能检查通过！")


if __name__ == "__main__":
    main()

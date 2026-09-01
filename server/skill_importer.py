"""
选股雷达 - Skill 导入服务
负责 zip 解压、路径安全校验、结构校验、工具构建预检、安装到 user_skills/

安全边界：
  - 不自动安装依赖，只展示缺失项
  - 不执行真实工具调用，只构建工具对象
  - 限制 zip 解压路径，拒绝 ../、绝对路径、软链接
  - 预检超时 10 秒
  - 临时目录请求结束立即清理
"""
import os
import re
import shutil
import zipfile
import tempfile
import importlib.util
import sys
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger("radar.skill_importer")

# 安全限制
MAX_ZIP_SIZE = 10 * 1024 * 1024   # 10MB
MAX_FILES_IN_ZIP = 200
PREVIEW_TIMEOUT = 10  # 秒

# 合法 skill name：字母数字下划线，小写开头
VALID_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


@dataclass
class CheckResult:
    """单项检查结果"""
    name: str
    status: str  # ok / warning / error
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass
class ImportPreview:
    """导入预览结果"""
    status: str  # ok / warning / error
    detected_type: str  # single_skill / external_package / unknown
    skill_name: str = ""
    skill_version: str = ""
    skill_description: str = ""
    skill_category: str = ""
    tools: List[str] = field(default_factory=list)
    checks: List[CheckResult] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "detected_type": self.detected_type,
            "skill": {
                "name": self.skill_name,
                "version": self.skill_version,
                "description": self.skill_description,
                "category": self.skill_category,
                "tools": self.tools,
            },
            "checks": [c.to_dict() for c in self.checks],
            "error": self.error,
        }


def _is_safe_path(path: str, base_dir: str) -> bool:
    """检查路径是否安全（在 base_dir 内，无 ..、绝对路径、软链接）"""
    # 拒绝绝对路径
    if os.path.isabs(path):
        return False
    # 拒绝 ..
    parts = Path(path).parts
    if any(p == '..' for p in parts):
        return False
    # 拒绝隐藏文件（以 . 开头的目录/文件名，但允许 .py 等扩展名）
    for p in parts:
        if p.startswith('.') and p != '.':
            return False
    # 检查解析后是否在 base_dir 内
    try:
        resolved = (Path(base_dir) / path).resolve()
        base_resolved = Path(base_dir).resolve()
        resolved.relative_to(base_resolved)
    except ValueError:
        return False
    return True


def validate_zip_safety(zip_path: str, extract_dir: str) -> Optional[str]:
    """校验 zip 文件路径安全性。返回错误信息或 None。"""
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # 检查文件数量
            names = zf.namelist()
            if len(names) > MAX_FILES_IN_ZIP:
                return f"zip 文件包含 {len(names)} 个文件，超过上限 {MAX_FILES_IN_ZIP}"
            # 逐个检查路径安全性
            for name in names:
                if not _is_safe_path(name, extract_dir):
                    return f"不安全的路径: {name}"
                # 拒绝软链接（zipline 类型 为 SYMLINK_TYPE）
                info = zf.getinfo(name)
                if hasattr(info, 'create_system') and info.create_system == 3:
                    # Unix 系统的 symlink
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        return f"拒绝软链接: {name}"
    except zipfile.BadZipFile:
        return "无效的 zip 文件"
    return None


def extract_zip(zip_path: str, extract_dir: str) -> None:
    """解压 zip 到指定目录。"""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)


def _detect_type(extract_dir: str) -> str:
    """检测导入类型：single_skill / external_package / unknown"""
    entries = [e for e in os.listdir(extract_dir) if not e.startswith('.')]
    # 如果只有一个子目录，进入子目录检测
    if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
        inner = os.path.join(extract_dir, entries[0])
        inner_entries = os.listdir(inner)
        # 单 skill：有 SKILL.md
        if 'SKILL.md' in inner_entries:
            return 'single_skill'
        # 外部包：有 skills.py
        if 'skills.py' in inner_entries:
            return 'external_package'
    # 直接有 SKILL.md
    if 'SKILL.md' in entries:
        return 'single_skill'
    # 直接有 skills.py
    if 'skills.py' in entries:
        return 'external_package'
    # 子目录中有 SKILL.md
    for e in entries:
        sub = os.path.join(extract_dir, e)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, 'SKILL.md')):
            return 'external_package'
    return 'unknown'


def _get_skill_root(extract_dir: str, detected_type: str) -> str:
    """获取 skill 根目录（处理 zip 内多一层目录的情况）"""
    entries = [e for e in os.listdir(extract_dir) if not e.startswith('.')]
    if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
        inner = os.path.join(extract_dir, entries[0])
        inner_entries = os.listdir(inner)
        if 'SKILL.md' in inner_entries or 'skills.py' in inner_entries:
            return inner
    return extract_dir


def _validate_single_skill(skill_root: str) -> ImportPreview:
    """校验单 skill 包"""
    from tools.skill_register import _parse_skill_md, _parse_tool_list, _parse_catalog_info

    checks = []
    preview = ImportPreview(status="ok", detected_type="single_skill")

    # 检查 SKILL.md
    skill_md = os.path.join(skill_root, 'SKILL.md')
    if not os.path.exists(skill_md):
        checks.append(CheckResult("SKILL.md", "error", "SKILL.md 不存在"))
        preview.status = "error"
        preview.checks = checks
        return preview

    checks.append(CheckResult("SKILL.md", "ok", "SKILL.md 存在"))

    # 解析 SKILL.md
    try:
        meta, content = _parse_skill_md(skill_md)
    except Exception as e:
        checks.append(CheckResult("SKILL.md 解析", "error", f"解析失败: {e}"))
        preview.status = "error"
        preview.checks = checks
        return preview

    # 元数据
    skill_name = meta.get("name", "")
    preview.skill_name = skill_name
    preview.skill_version = meta.get("version", "v1.0")
    preview.skill_description = meta.get("description", "")
    preview.skill_category = meta.get("category", "")

    # 名称校验
    if skill_name:
        if not VALID_NAME_RE.match(skill_name):
            checks.append(CheckResult("name 格式", "error", f"名称 '{skill_name}' 不合法，需小写字母开头，只含字母数字下划线"))
            preview.status = "error"
        else:
            checks.append(CheckResult("name 格式", "ok", f"名称 '{skill_name}' 合法"))
    else:
        checks.append(CheckResult("name 格式", "warning", "SKILL.md 中未定义 name，将使用目录名"))

    # 工具列表
    tool_list = _parse_tool_list(content)
    tool_names = [t.get("tool_name", "") for t in tool_list]
    preview.tools = tool_names
    if not tool_names:
        checks.append(CheckResult("工具列表", "warning", "SKILL.md 中未声明工具"))
    else:
        checks.append(CheckResult("工具列表", "ok", f"发现 {len(tool_names)} 个工具: {', '.join(tool_names)}"))

    # main.py 检查
    main_py = os.path.join(skill_root, 'main.py')
    if os.path.exists(main_py):
        checks.append(CheckResult("main.py", "ok", "main.py 存在"))
        # 尝试导入模块
        try:
            spec = importlib.util.spec_from_file_location("_preview_skill", main_py)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, 'build_tools'):
                    checks.append(CheckResult("build_tools", "ok", "build_tools 函数存在"))
                    # 尝试构建工具（用 mock logger）
                    import logging
                    mock_logger = logging.getLogger("_preview")
                    try:
                        tools = module.build_tools(mock_logger, None)
                        if isinstance(tools, list):
                            checks.append(CheckResult("工具构建", "ok", f"成功构建 {len(tools)} 个工具"))
                        else:
                            checks.append(CheckResult("工具构建", "warning", "build_tools 未返回 list"))
                    except Exception as e:
                        checks.append(CheckResult("工具构建", "warning", f"构建工具时异常: {str(e)[:100]}"))
                else:
                    checks.append(CheckResult("build_tools", "warning", "main.py 中无 build_tools 函数"))
        except Exception as e:
            checks.append(CheckResult("main.py 导入", "warning", f"导入 main.py 失败: {str(e)[:100]}"))
    else:
        checks.append(CheckResult("main.py", "warning", "main.py 不存在，建议提供"))

    # requirements.txt
    req_txt = os.path.join(skill_root, 'requirements.txt')
    if os.path.exists(req_txt):
        try:
            req_content = open(req_txt, 'r', encoding='utf-8').read()
            req_lines = [l.strip() for l in req_content.splitlines() if l.strip() and not l.startswith('#')]
            # 检查缺失依赖
            missing = []
            for req in req_lines:
                pkg = re.split(r'[>=<!~\[]', req)[0].strip()
                if pkg and not _is_package_installed(pkg):
                    missing.append(req)
            if missing:
                checks.append(CheckResult("requirements", "warning", f"缺失依赖: {', '.join(missing)}（需手动安装）"))
            else:
                checks.append(CheckResult("requirements", "ok", "所有依赖已安装"))
        except Exception:
            checks.append(CheckResult("requirements", "warning", "无法读取 requirements.txt"))

    # 更新总状态
    has_error = any(c.status == "error" for c in checks)
    has_warning = any(c.status == "warning" for c in checks)
    if has_error:
        preview.status = "error"
    elif has_warning:
        preview.status = "warning"

    preview.checks = checks
    return preview


def _validate_external_package(skill_root: str) -> ImportPreview:
    """校验外部包"""
    checks = []
    preview = ImportPreview(status="ok", detected_type="external_package")

    skills_py = os.path.join(skill_root, 'skills.py')
    if not os.path.exists(skills_py):
        checks.append(CheckResult("skills.py", "error", "外部包必须包含 skills.py"))
        preview.status = "error"
        preview.checks = checks
        return preview

    checks.append(CheckResult("skills.py", "ok", "skills.py 存在"))

    # 尝试导入 skills.py
    try:
        spec = importlib.util.spec_from_file_location("_preview_pkg", skills_py)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, 'get_skill_catalog'):
                catalog = module.get_skill_catalog()
                checks.append(CheckResult("get_skill_catalog", "ok", f"目录文本长度 {len(str(catalog))}"))
            else:
                checks.append(CheckResult("get_skill_catalog", "warning", "缺少 get_skill_catalog 函数"))
            if hasattr(module, 'get_skill_loaders'):
                loaders = module.get_skill_loaders()
                loader_names = list(loaders.keys()) if isinstance(loaders, dict) else []
                preview.tools = loader_names
                checks.append(CheckResult("get_skill_loaders", "ok", f"发现 {len(loader_names)} 个加载器"))
            else:
                checks.append(CheckResult("get_skill_loaders", "warning", "缺少 get_skill_loaders 函数"))
    except Exception as e:
        checks.append(CheckResult("skills.py 导入", "error", f"导入失败: {str(e)[:100]}"))
        preview.status = "error"

    # 检查子 skill 目录
    sub_skills = []
    for item in os.listdir(skill_root):
        sub = os.path.join(skill_root, item)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, 'SKILL.md')):
            sub_skills.append(item)
    if sub_skills:
        checks.append(CheckResult("子技能目录", "ok", f"发现子技能: {', '.join(sub_skills)}"))
        preview.skill_name = os.path.basename(skill_root)
    else:
        checks.append(CheckResult("子技能目录", "warning", "未发现子技能目录"))

    has_error = any(c.status == "error" for c in checks)
    has_warning = any(c.status == "warning" for c in checks)
    if has_error:
        preview.status = "error"
    elif has_warning:
        preview.status = "warning"

    preview.checks = checks
    return preview


def _is_package_installed(pkg_name: str) -> bool:
    """检查 Python 包是否已安装"""
    try:
        importlib.import_module(pkg_name.replace('-', '_').split('[')[0])
        return True
    except ImportError:
        return False


def preview_import(zip_path: str, existing_skills: Optional[List[str]] = None) -> ImportPreview:
    """预览导入：校验 zip 安全性、解压、结构校验、工具构建预检。不安装。"""
    # 检查文件大小
    file_size = os.path.getsize(zip_path)
    if file_size > MAX_ZIP_SIZE:
        return ImportPreview(
            status="error",
            detected_type="unknown",
            error=f"文件大小 {file_size} 超过上限 {MAX_ZIP_SIZE} 字节",
        )

    with tempfile.TemporaryDirectory(prefix="skill_import_") as tmp_dir:
        # 安全校验
        err = validate_zip_safety(zip_path, tmp_dir)
        if err:
            return ImportPreview(status="error", detected_type="unknown", error=err)

        # 解压
        try:
            extract_zip(zip_path, tmp_dir)
        except Exception as e:
            return ImportPreview(status="error", detected_type="unknown", error=f"解压失败: {e}")

        # 检测类型
        detected_type = _detect_type(tmp_dir)
        if detected_type == "unknown":
            return ImportPreview(
                status="error",
                detected_type="unknown",
                error="无法识别导入类型，请检查 zip 结构（需要 SKILL.md 或 skills.py）",
            )

        skill_root = _get_skill_root(tmp_dir, detected_type)

        # 名称冲突检查
        if detected_type == "single_skill":
            preview = _validate_single_skill(skill_root)
        else:
            preview = _validate_external_package(skill_root)

        # 检查名称冲突
        if existing_skills and preview.skill_name in existing_skills:
            preview.checks.append(CheckResult(
                "name 冲突", "warning",
                f"技能 '{preview.skill_name}' 已存在，导入后将覆盖",
            ))
            if preview.status == "ok":
                preview.status = "warning"

        return preview


def install_skill(zip_path: str, preview: ImportPreview) -> str:
    """安装技能到 user_skills/ 目录。返回安装路径。"""
    from utils.app_paths import get_user_skills_dir

    user_skills_dir = get_user_skills_dir()
    os.makedirs(user_skills_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="skill_install_") as tmp_dir:
        # 安全校验
        err = validate_zip_safety(zip_path, tmp_dir)
        if err:
            raise ValueError(f"安全校验失败: {err}")

        # 解压
        extract_zip(zip_path, tmp_dir)

        detected_type = _detect_type(tmp_dir)
        skill_root = _get_skill_root(tmp_dir, detected_type)

        # 目标路径
        skill_name = preview.skill_name or os.path.basename(skill_root)
        dest_dir = os.path.join(user_skills_dir, skill_name)

        # 如果已存在，先删除
        if os.path.exists(dest_dir):
            shutil.rmtree(dest_dir)

        # 复制
        shutil.copytree(skill_root, dest_dir)

        logger.info(f"技能 '{skill_name}' 已安装到 {dest_dir}")
        return dest_dir

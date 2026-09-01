"""skill_importer 导入服务测试"""
import sys, os
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _make_skill_zip(tmp_dir: str, skill_name: str = "test_skill", with_main: bool = True) -> str:
    """创建一个包含 skill 的 zip 文件"""
    skill_dir = Path(tmp_dir) / skill_name
    skill_dir.mkdir()

    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {skill_name}
version: v1.0
description: 测试技能
category: 测试
---

# {skill_name}
## 基础信息
- 版本：v1.0
- 描述：测试技能

## 目录层信息
- 关键参数：symbol（必填）
- 核心目标：测试

## 工具列表
### 工具1：get_data
- 功能：获取数据
- 调用入口：main.py
- 参数约束：
  - symbol: str（必填）- 股票代码

## 使用指南
- 用于测试。
""",
        encoding="utf-8",
    )

    if with_main:
        (skill_dir / "main.py").write_text(
            """from pydantic import BaseModel


class GetDataParams(BaseModel):
    symbol: str


def get_data(symbol: str) -> dict:
    return {"symbol": symbol}


TOOL_REGISTRY = {
    "get_data": (get_data, GetDataParams),
}


def build_tools(logger, memory_mgr):
    from tools.skill_register import ToolMeta, ToolParamMeta
    return [
        ToolMeta(
            tool_name="get_data",
            description="获取数据",
            tool_func=get_data,
            param_model=GetDataParams,
        )
    ]
""",
            encoding="utf-8",
        )

    zip_path = os.path.join(tmp_dir, f"{skill_name}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in skill_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(tmp_dir))
    return zip_path


def _make_external_package_zip(tmp_dir: str, package_name: str = "sample_pack") -> str:
    package_dir = Path(tmp_dir) / package_name
    child_dir = package_dir / "sample_child"
    child_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (child_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "skills.py").write_text(
        '''def get_skill_catalog():
    return "sample_child: 测试外部技能"


def build_sample_child(logger, memory_mgr):
    from .sample_child.sample_child import build_tools
    return build_tools(logger, memory_mgr)


def get_skill_loaders():
    return {"sample_child": build_sample_child}
''',
        encoding="utf-8",
    )
    (child_dir / "SKILL.md").write_text(
        """---
name: sample_child
version: v1.0
description: 测试外部子技能
---
# sample_child
## 工具列表
### 工具1：sample_tool
- 功能：测试 package 相对导入
""",
        encoding="utf-8",
    )
    (child_dir / "sample_child.py").write_text(
        '''def build_tools(logger, memory_mgr):
    return ["built-from-user-package"]
''',
        encoding="utf-8",
    )

    zip_path = os.path.join(tmp_dir, f"{package_name}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in package_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(tmp_dir))
    return zip_path


def test_validate_zip_safety_ok():
    """正常 zip 应通过安全校验"""
    from server.skill_importer import validate_zip_safety

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_skill_zip(tmp)
        with tempfile.TemporaryDirectory() as extract_dir:
            err = validate_zip_safety(zip_path, extract_dir)
            assert err is None


def test_validate_zip_safety_dotdot():
    """包含 ../ 的 zip 应被拒绝"""
    from server.skill_importer import validate_zip_safety

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "bad.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("../evil.txt", "bad")
        with tempfile.TemporaryDirectory() as extract_dir:
            err = validate_zip_safety(zip_path, extract_dir)
            assert err is not None
            assert "不安全" in err


def test_validate_zip_safety_absolute_path():
    """包含绝对路径的 zip 应被拒绝"""
    from server.skill_importer import validate_zip_safety

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "bad.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("/etc/passwd", "bad")
        with tempfile.TemporaryDirectory() as extract_dir:
            err = validate_zip_safety(zip_path, extract_dir)
            assert err is not None


def test_validate_zip_safety_hidden_dir():
    """包含隐藏目录的 zip 应被拒绝"""
    from server.skill_importer import validate_zip_safety

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "bad.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr(".hidden/evil.py", "bad")
        with tempfile.TemporaryDirectory() as extract_dir:
            err = validate_zip_safety(zip_path, extract_dir)
            assert err is not None


def test_preview_single_skill():
    """预览单 skill 应返回正确信息"""
    from server.skill_importer import preview_import

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_skill_zip(tmp, "my_skill")
        result = preview_import(zip_path)

        assert result.status == "ok"
        assert result.detected_type == "single_skill"
        assert result.skill_name == "my_skill"
        assert "get_data" in result.tools


def test_preview_without_main():
    """没有 main.py 的 skill 应返回 warning"""
    from server.skill_importer import preview_import

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_skill_zip(tmp, "no_main_skill", with_main=False)
        result = preview_import(zip_path)

        assert result.detected_type == "single_skill"
        # 应该有 warning（main.py 不存在）
        warnings = [c for c in result.checks if c.status == "warning"]
        assert len(warnings) > 0


def test_preview_invalid_zip():
    """无效 zip 应返回 error"""
    from server.skill_importer import preview_import

    with tempfile.TemporaryDirectory() as tmp:
        bad_zip = os.path.join(tmp, "bad.zip")
        with open(bad_zip, 'w') as f:
            f.write("not a zip")
        result = preview_import(bad_zip)
        assert result.status == "error"


def test_preview_unknown_type():
    """无法识别类型的 zip 应返回 error"""
    from server.skill_importer import preview_import

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "unknown.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("readme.txt", "no skill here")
        result = preview_import(zip_path)
        assert result.status == "error"
        assert result.detected_type == "unknown"


def test_preview_name_conflict():
    """名称冲突应返回 warning"""
    from server.skill_importer import preview_import

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_skill_zip(tmp, "conflict_skill")
        result = preview_import(zip_path, existing_skills=["conflict_skill"])
        assert result.status == "warning"
        conflicts = [c for c in result.checks if "冲突" in c.name]
        assert len(conflicts) > 0


def test_preview_bad_name():
    """名称不合法应返回 error"""
    from server.skill_importer import preview_import

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_skill_zip(tmp, "Bad-Name!")
        result = preview_import(zip_path)
        # 名称不合法的 check
        name_checks = [c for c in result.checks if "name" in c.name.lower()]
        # 如果 SKILL.md 中的 name 是 "Bad-Name!" 则应报错
        # 但因为 name 是目录名，SKILL.md 中写的是 "Bad-Name!"
        # 需要确认 SKILL.md 中的 name 字段
        assert any(c.status == "error" for c in name_checks)


def test_install_skill():
    """安装技能应复制到 user_skills 目录"""
    from server.skill_importer import preview_import, install_skill

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_skill_zip(tmp, "install_test")
        preview = preview_import(zip_path)
        assert preview.status == "ok"

        # 安装到临时 user_skills
        user_skills_dir = Path(tmp) / "user_skills"
        user_skills_dir.mkdir()

        # monkeypatch get_user_skills_dir
        import server.skill_importer as si
        original = si.get_user_skills_dir if hasattr(si, 'get_user_skills_dir') else None

        import utils.app_paths as ap
        orig_func = ap.get_user_skills_dir
        ap.get_user_skills_dir = lambda: str(user_skills_dir)
        try:
            install_path = install_skill(zip_path, preview)
            assert os.path.exists(install_path)
            assert os.path.exists(os.path.join(install_path, "SKILL.md"))
        finally:
            ap.get_user_skills_dir = orig_func


def test_external_package_import_rescan_and_build():
    """外部包安装后应能从 user_skills 发现并执行 loader。"""
    from server.skill_importer import install_skill, preview_import
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_external_package_zip(tmp)
        preview = preview_import(zip_path)
        assert preview.status == "ok"
        assert preview.detected_type == "external_package"
        assert preview.tools == ["sample_child"]

        user_skills_dir = Path(tmp) / "user_skills"
        user_skills_dir.mkdir()
        import utils.app_paths as app_paths

        original = app_paths.get_user_skills_dir
        app_paths.get_user_skills_dir = lambda: str(user_skills_dir)
        try:
            install_skill(zip_path, preview)
            registry = SkillRegister()
            registry.auto_discover([str(user_skills_dir)])
        finally:
            app_paths.get_user_skills_dir = original

        skill = registry.get_skill_unchecked("sample_child")
        assert skill is not None
        assert skill.source == "user"
        assert skill.build_tools_func is not None
        assert skill.build_tools_func(None, None) == ["built-from-user-package"]


def test_is_safe_path_rejects_symlinks():
    """_is_safe_path 应拒绝隐藏的可执行文件"""
    from server.skill_importer import _is_safe_path

    with tempfile.TemporaryDirectory() as tmp:
        assert _is_safe_path("skills/my_skill/SKILL.md", tmp) is True
        assert _is_safe_path("../evil", tmp) is False
        assert _is_safe_path("/etc/passwd", tmp) is False
        assert _is_safe_path(".hidden/file.py", tmp) is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

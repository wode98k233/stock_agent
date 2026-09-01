import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_skill_register_instance():
    print("=" * 60)
    print("TC-SR-01: SkillRegister 实例化")
    print("=" * 60)

    from tools.skill_register import SkillRegister

    sr = SkillRegister()
    assert sr._initialized == False
    assert len(sr._registry) == 0
    print("[OK] 实例化正确")


def test_skill_register_reset():
    print("\n" + "=" * 60)
    print("TC-SR-02: SkillRegister reset")
    print("=" * 60)

    from tools.skill_register import SkillRegister

    sr = SkillRegister()
    sr._registry["test"] = "dummy"
    sr._initialized = True
    sr.reset()
    assert len(sr._registry) == 0
    assert sr._initialized == False
    print("[OK] reset 正确")


def test_skill_register_two_instances_isolated():
    print("\n" + "=" * 60)
    print("TC-SR-03: 两个实例互不干扰")
    print("=" * 60)

    from tools.skill_register import SkillRegister

    sr1 = SkillRegister()
    sr2 = SkillRegister()
    sr1._registry["a"] = "1"
    sr2._registry["b"] = "2"
    assert "a" in sr1._registry
    assert "a" not in sr2._registry
    assert "b" in sr2._registry
    assert "b" not in sr1._registry
    print("[OK] 实例隔离正确")


def test_eastmoney_sub_skills_only_expose_declared_tool():
    """外部子 skill 的元数据不能注入同包其它工具。"""
    from tools.skill_register import SkillRegister
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        external_root = Path(tmp) / "other_skills"
        package_dir = external_root / "eastmoney"
        package_dir.mkdir(parents=True)

        (package_dir / "skills.py").write_text(
            """
from pydantic import BaseModel


class QueryParams(BaseModel):
    query: str


def _tool_a(query: str):
    return "a"


def _tool_b(query: str):
    return "b"


TOOL_REGISTRY = {
    "mx_data_query": (_tool_a, QueryParams),
    "mx_search_news": (_tool_b, QueryParams),
}


def get_skill_catalog():
    return "## fake eastmoney"


def get_skill_loaders():
    return {
        "mx_data": lambda logger, memory_mgr: [],
        "mx_search": lambda logger, memory_mgr: [],
    }
""",
            encoding="utf-8",
        )

        for skill_name, tool_name in {
            "mx_data": "mx_data_query",
            "mx_search": "mx_search_news",
        }.items():
            skill_dir = package_dir / skill_name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"""---
name: {skill_name}
version: v1.0
description: fake
---

# {skill_name}
## 基础信息
- 版本：v1.0
- 描述：fake
- 适用场景：fake

## 关联文件
- 工具注册入口：./{skill_name}.py

## 目录层信息
- 关键参数：query（必填）
- 核心目标：fake

## 工具列表
### 工具1：{tool_name}
- 功能：fake
- 调用入口：{tool_name}
- 参数约束：
  - query: str（必填）- fake
""",
                encoding="utf-8",
            )

        sr = SkillRegister()
        sr.auto_discover([str(external_root)])

        expected = {
            "mx_data": ["mx_data_query"],
            "mx_search": ["mx_search_news"],
        }

        for skill_name, tool_names in expected.items():
            meta = sr.get_skill(skill_name)
            assert meta is not None, f"缺少外部 skill: {skill_name}"
            assert [tool.tool_name for tool in meta.tools] == tool_names
            assert meta.tool_registry is not None
            assert sorted(meta.tool_registry.keys()) == tool_names


if __name__ == "__main__":
    try:
        test_skill_register_instance()
        test_skill_register_reset()
        test_skill_register_two_instances_isolated()
        test_eastmoney_sub_skills_only_expose_declared_tool()
        print("\n" + "=" * 60)
        print("[PASS] All SkillRegister tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

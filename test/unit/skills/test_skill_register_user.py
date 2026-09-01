"""SkillRegister 用户技能目录扫描测试"""
import sys, os
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_user_skills_discovered():
    """user_skills 目录下的技能应被自动发现"""
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "user_skills"
        skill_dir = user_dir / "my_test_skill"
        skill_dir.mkdir(parents=True)

        (skill_dir / "SKILL.md").write_text(
            """---
name: my_test_skill
version: v1.0
description: 用户导入的测试技能
category: 测试
---

# my_test_skill
## 基础信息
- 版本：v1.0
- 描述：用户导入的测试技能
- 适用场景：测试

## 目录层信息
- 关键参数：symbol（必填）
- 核心目标：测试

## 工具列表
### 工具1：get_test_data
- 功能：获取测试数据
- 调用入口：main.py.build_tools
- 参数约束：
  - symbol: str（必填）- 股票代码

## 使用指南
- 用于测试。
""",
            encoding="utf-8",
        )

        sr = SkillRegister()
        sr.auto_discover([str(user_dir)])

        meta = sr.get_skill_unchecked("my_test_skill")
        assert meta is not None, "用户技能应被发现"
        assert meta.source == "user"
        assert meta.enabled is True
        assert len(meta.tools) == 1
        assert meta.tools[0].tool_name == "get_test_data"


def test_user_skills_source_field():
    """内置/外部/用户技能的 source 字段应正确"""
    from tools.skill_register import SkillRegister, SkillMeta

    sr = SkillRegister()
    sr.register_skill(SkillMeta(skill_name="builtin_skill", source="internal"))
    sr.register_skill(SkillMeta(skill_name="ext_skill", source="external"))
    sr.register_skill(SkillMeta(skill_name="user_skill", source="user"))

    assert sr.get_skill_unchecked("builtin_skill").source == "internal"
    assert sr.get_skill_unchecked("ext_skill").source == "external"
    assert sr.get_skill_unchecked("user_skill").source == "user"


def test_rescan_clears_and_rediscovers():
    """rescan 应清空注册表并重新发现"""
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "user_skills"
        skill_dir = user_dir / "rescan_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: rescan_skill
version: v1.0
---
# rescan_skill
## 工具列表
### 工具1：t1
- 功能：test
""",
            encoding="utf-8",
        )

        sr = SkillRegister()
        sr.auto_discover([str(user_dir)])
        assert sr.get_skill_unchecked("rescan_skill") is not None

        # 添加新技能
        skill_dir2 = user_dir / "new_skill"
        skill_dir2.mkdir(parents=True)
        (skill_dir2 / "SKILL.md").write_text(
            """---
name: new_skill
version: v1.0
---
# new_skill
## 工具列表
### 工具1：t2
- 功能：test
""",
            encoding="utf-8",
        )

        # rescan 使用默认目录，这里手动 reset + auto_discover 来测试重新发现
        sr.reset()
        sr.auto_discover([str(user_dir)])
        # 重新发现后应该看到两个技能
        assert sr.get_skill_unchecked("rescan_skill") is not None
        assert sr.get_skill_unchecked("new_skill") is not None


def test_reset_clears_deferred_loader_state():
    """reset 应清除延迟加载状态，保证后续 rescan 可重新 warmup。"""
    from tools.skill_register import SkillRegister

    sr = SkillRegister()
    sr._pending_modules.append((object(), "main.py", "test.module"))
    sr._pending_external_packages.append(("package", "user"))
    sr._modules_warmed = True

    sr.reset()

    assert sr._pending_modules == []
    assert sr._pending_external_packages == []
    assert sr._modules_warmed is False


def test_delete_user_skill():
    """删除用户技能应移除文件和注册"""
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "user_skills"
        skill_dir = user_dir / "del_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: del_skill
version: v1.0
---
# del_skill
## 工具列表
### 工具1：t1
- 功能：test
""",
            encoding="utf-8",
        )

        sr = SkillRegister()
        sr.auto_discover([str(user_dir)])
        assert sr.get_skill_unchecked("del_skill") is not None

        result = sr.delete_user_skill("del_skill")
        assert result is True
        assert sr.get_skill_unchecked("del_skill") is None
        assert not skill_dir.exists()


def test_cannot_delete_builtin_skill():
    """不允许删除内置技能"""
    from tools.skill_register import SkillRegister, SkillMeta

    sr = SkillRegister()
    sr.register_skill(SkillMeta(skill_name="builtin", source="internal"))
    result = sr.delete_user_skill("builtin")
    assert result is False
    assert sr.get_skill_unchecked("builtin") is not None


def test_set_skill_enabled():
    """启用/禁用技能应更新状态"""
    from unittest.mock import patch
    from tools.skill_register import SkillRegister, SkillMeta

    sr = SkillRegister()
    sr.register_skill(SkillMeta(skill_name="toggle_test", source="user", enabled=True))

    # mock 掉持久化，不往真实 index.json 写数据
    with patch.object(sr, '_persist_skill_enabled'):
        result = sr.set_skill_enabled("toggle_test", False)
        assert result is True
        assert sr.get_skill_unchecked("toggle_test").enabled is False

        result = sr.set_skill_enabled("toggle_test", True)
        assert result is True
        assert sr.get_skill_unchecked("toggle_test").enabled is True


def test_get_all_skills_unchecked():
    """get_all_skills_unchecked 应返回所有技能（含禁用）"""
    from tools.skill_register import SkillRegister, SkillMeta

    sr = SkillRegister()
    sr.register_skill(SkillMeta(skill_name="a", source="internal", enabled=True))
    sr.register_skill(SkillMeta(skill_name="b", source="user", enabled=False))

    all_skills = sr.get_all_skills_unchecked()
    assert "a" in all_skills
    assert "b" in all_skills

    enabled_only = sr.get_all_skills()
    assert "a" in enabled_only
    assert "b" not in enabled_only


def test_get_skill_detail():
    """get_skill_detail 应返回完整详情"""
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "user_skills"
        skill_dir = user_dir / "detail_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: detail_skill
version: v2.0
description: 详情测试技能
category: 测试
---

# detail_skill
## 基础信息
- 版本：v2.0
- 描述：详情测试技能

## 目录层信息
- 关键参数：code
- 核心目标：测试详情

## 工具列表
### 工具1：get_info
- 功能：获取信息
- 调用入口：main.py
- 参数约束：
  - code: str（必填）- 代码

## 使用指南
### 适用场景
- 查询信息。
""",
            encoding="utf-8",
        )

        sr = SkillRegister()
        sr.auto_discover([str(user_dir)])

        detail = sr.get_skill_detail("detail_skill")
        assert detail is not None
        assert detail["name"] == "detail_skill"
        assert detail["version"] == "v2.0"
        assert detail["source"] == "user"
        assert len(detail["tools"]) == 1
        assert detail["tools"][0]["tool_name"] == "get_info"
        assert detail["catalog_info"]["key_params"] == ["code"]
        assert detail["catalog_info"]["target"] == "测试详情"
        assert "适用场景" in detail["usage_guide"]


def test_buildable_true_when_build_tools_func_exists():
    """有 build_tools_func 的 skill，其工具的 buildable 应为 True"""
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "user_skills"
        skill_dir = user_dir / "buildable_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: buildable_skill
version: v1.0
---
# buildable_skill
## 工具列表
### 工具1：do_work
- 功能：执行工作
""",
            encoding="utf-8",
        )
        (skill_dir / "main.py").write_text(
            """def build_tools(logger, memory_mgr):
    return []
""",
            encoding="utf-8",
        )

        sr = SkillRegister()
        sr.auto_discover([str(user_dir)])

        detail = sr.get_skill_detail("buildable_skill")
        assert detail is not None
        assert detail["has_build_tools"] is True
        assert detail["tools"][0]["buildable"] is True


def test_buildable_false_when_no_build_tools():
    """无 build_tools_func 且无 tool_func 的 skill，buildable 应为 False"""
    from tools.skill_register import SkillRegister

    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "user_skills"
        skill_dir = user_dir / "no_build_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: no_build_skill
version: v1.0
---
# no_build_skill
## 工具列表
### 工具1：do_stuff
- 功能：做事情
""",
            encoding="utf-8",
        )

        sr = SkillRegister()
        sr.auto_discover([str(user_dir)])

        detail = sr.get_skill_detail("no_build_skill")
        assert detail is not None
        assert detail["has_build_tools"] is False
        assert detail["tools"][0]["buildable"] is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

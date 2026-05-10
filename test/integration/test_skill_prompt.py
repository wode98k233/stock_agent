"""
SkillPromptBuilder 集成测试

验证 SkillPromptBuilder 的 prompt 构建功能：
1. build_catalog_prompt — 全量技能目录
2. build_tools_detail_prompt — 单技能详细指南
3. usage_guide 解析

运行方式：
  pytest test/integration/test_skill_prompt.py -v -m integration
"""
import os
import sys
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture
def registry():
    """创建 SkillRegistry 实例（需要 skill 自动发现）"""
    from tools.skills import SkillRegistry
    from tools.skill_register import SkillRegister
    from utils.logger import get_logger
    from utils.memory import MemoryManager

    logger, _ = get_logger("test-skill-prompt")
    memory = MemoryManager(logger)
    skill_register = SkillRegister()
    skill_register.auto_discover()
    return SkillRegistry(logger, memory, skill_register)


@pytest.mark.integration
def test_build_catalog_prompt(registry):
    """build_catalog_prompt 应返回非空字符串，包含已知 skill 名"""
    from tools.skills import SkillPromptBuilder

    catalog = SkillPromptBuilder.build_catalog_prompt(registry)
    assert isinstance(catalog, str)
    assert len(catalog) > 100, f"catalog prompt 过短: {len(catalog)} 字符"
    # 应包含至少一个已知 skill 名
    known_skills = ["stock_query", "technical_analysis", "mx_data"]
    found = [s for s in known_skills if s in catalog]
    assert len(found) >= 1, f"catalog 中未找到任何已知 skill，只找到: {found}"


@pytest.mark.integration
def test_build_tools_detail_prompt_stock_query(registry):
    """build_tools_detail_prompt(stock_query) 应返回使用指南"""
    from tools.skills import SkillPromptBuilder

    guide = SkillPromptBuilder.build_tools_detail_prompt(registry, "stock_query")
    assert isinstance(guide, str)
    assert len(guide) > 50, f"stock_query 指南过短: {len(guide)} 字符"


@pytest.mark.integration
def test_build_tools_detail_prompt_mx_data(registry):
    """build_tools_detail_prompt(mx_data) 应返回使用指南"""
    from tools.skills import SkillPromptBuilder

    guide = SkillPromptBuilder.build_tools_detail_prompt(registry, "mx_data")
    assert isinstance(guide, str)
    assert len(guide) > 50, f"mx_data 指南过短: {len(guide)} 字符"


@pytest.mark.integration
def test_usage_guide_parsing(registry):
    """usage_guide 应能正确解析为结构化内容"""
    for skill_name in ["stock_query", "technical_analysis"]:
        usage_guide = registry.get_skill_usage_guide(skill_name)
        if usage_guide:
            assert isinstance(usage_guide, str)
            assert len(usage_guide) > 0, f"{skill_name} usage_guide 为空字符串"


if __name__ == "__main__":
    import traceback
    from tools.skills import SkillRegistry, SkillPromptBuilder
    from tools.skill_register import SkillRegister
    from utils.logger import get_logger
    from utils.memory import MemoryManager

    logger, _ = get_logger("test")
    memory = MemoryManager(logger)
    skill_register = SkillRegister()
    skill_register.auto_discover()
    reg = SkillRegistry(logger, memory, skill_register)

    tests = [
        lambda: test_build_catalog_prompt(reg),
        lambda: test_build_tools_detail_prompt_stock_query(reg),
        lambda: test_build_tools_detail_prompt_mx_data(reg),
        lambda: test_usage_guide_parsing(reg),
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"[PASS] {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test.__name__}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("[OK] 所有 SkillPrompt 测试通过!")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)

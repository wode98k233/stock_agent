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


if __name__ == "__main__":
    try:
        test_skill_register_instance()
        test_skill_register_reset()
        test_skill_register_two_instances_isolated()
        print("\n" + "=" * 60)
        print("[PASS] All SkillRegister tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

"""
Test: 用户画像记忆 (原 IntentMemoryManager → 已迁移至 UserProfileManager)
验证: memory/user_profile.py - UserProfileManager
"""
import os, tempfile, shutil
from unittest.mock import patch


def test_intent_entity_extraction():
    print("=" * 60)
    print("TC-Intent-01: 实体提取准确性")
    print("=" * 60)

    from memory.user_profile import UserProfileManager

    temp = tempfile.mkdtemp()
    try:
        with patch("memory.user_profile.get_user_profile_dir", return_value=temp):
            mgr = UserProfileManager(user_id="test")

            sectors, stocks = mgr.extract_entities(
                "今天宁德时代涨了很多，电池板块整体表现强劲"
            )
            print(f"  板块: {sectors}")
            print(f"  个股: {stocks}")
            assert "电池" in sectors, f"板块缺少 '电池', got {sectors}"
            print("[OK] 板块提取正确")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_intent_weight_decay():
    print("\n" + "=" * 60)
    print("TC-Intent-02: 权重衰减")
    print("=" * 60)

    from memory.user_profile import UserProfileManager

    temp = tempfile.mkdtemp()
    try:
        with patch("memory.user_profile.get_user_profile_dir", return_value=temp):
            mgr = UserProfileManager(user_id="test")
            mgr.update("分析银行", "结果", sectors=["银行"], stocks=[])
            w1 = mgr.profile.watched_sectors.get("银行", 0)
            mgr.update("分析券商", "结果", sectors=["券商"], stocks=[])
            w2 = mgr.profile.watched_sectors.get("银行", 0)
            print(f"  初始权重: {w1}")
            print(f"  一次衰减后: {w2} (期望 ~{w1 * 0.95:.3f})")
            assert abs(w2 - w1 * 0.95) < 0.01, f"衰减不正确: {w2} vs {w1 * 0.95}"
            print("[OK]")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_intent_threshold_removal():
    print("\n" + "=" * 60)
    print("TC-Intent-03: 低于阈值删除")
    print("=" * 60)

    from memory.user_profile import UserProfileManager

    temp = tempfile.mkdtemp()
    try:
        with patch("memory.user_profile.get_user_profile_dir", return_value=temp):
            mgr = UserProfileManager(user_id="test")
            mgr.update("分析银行", "结果", sectors=["银行"], stocks=[])

            for _ in range(50):
                mgr.update("分析其他", "结果", sectors=["其他"], stocks=[])

            final_weight = mgr.profile.watched_sectors.get("银行", 0)
            print(f"  50次无银行更新后，'银行' 权重: {final_weight}")
            assert "银行" not in mgr.profile.watched_sectors or final_weight < 0.1, \
                f"银行应该被删除或低于阈值, 仍有权重: {final_weight}"
            print("[OK]")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_intent_persistence():
    print("\n" + "=" * 60)
    print("TC-Intent-04: JSON 持久化")
    print("=" * 60)

    from memory.user_profile import UserProfileManager

    temp = tempfile.mkdtemp()
    try:
        with patch("memory.user_profile.get_user_profile_dir", return_value=temp):
            mgr1 = UserProfileManager(user_id="persist_test")
            mgr1.update("分析银行板块", "银行板块强势", sectors=["银行"], stocks=["招商银行"])
            mgr1.update("分析券商板块", "券商震荡", sectors=["券商"], stocks=["中信证券"])

            mgr2 = UserProfileManager(user_id="persist_test")
            sectors2 = list(mgr2.profile.watched_sectors.keys())
            stocks2 = list(mgr2.profile.watched_stocks.keys())

            print(f"  重启后板块: {sectors2}")
            print(f"  重启后个股: {stocks2}")
            assert "银行" in sectors2
            assert "券商" in sectors2
            assert "招商银行" in stocks2 or "招商" in stocks2
            print("[OK]")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_intent_context_hint():
    print("\n" + "=" * 60)
    print("TC-Intent-05: get_context_hint 格式")
    print("=" * 60)

    from memory.user_profile import UserProfileManager

    temp = tempfile.mkdtemp()
    try:
        with patch("memory.user_profile.get_user_profile_dir", return_value=temp):
            mgr = UserProfileManager(user_id="hint_test")
            mgr.update("分析银行", "强势", sectors=["银行"], stocks=["招商银行"])
            mgr.update("分析券商", "震荡", sectors=["券商"], stocks=["中信证券"])

            hint = mgr.get_context_hint()
            print(f"  Hint:\n{hint}")
            assert "银行" in hint
            assert "券商" in hint
            assert "用户画像" in hint
            print("[OK]")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_intent_preferred_depth():
    print("\n" + "=" * 60)
    print("TC-Intent-06: preferred_depth 推断")
    print("=" * 60)

    from memory.user_profile import UserProfileManager

    temp = tempfile.mkdtemp()
    try:
        with patch("memory.user_profile.get_user_profile_dir", return_value=temp):
            mgr = UserProfileManager(user_id="depth_test")
            for _ in range(3):
                mgr.update("大盘怎么样", "讨论", question_type="discussion")

            assert mgr.profile.preferred_depth == "simple", \
                f"期望 'simple', got '{mgr.profile.preferred_depth}'"
            print(f"  偏好深度: {mgr.profile.preferred_depth}")
            print("[OK]")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_intent_entity_extraction()
        test_intent_weight_decay()
        test_intent_threshold_removal()
        test_intent_persistence()
        test_intent_context_hint()
        test_intent_preferred_depth()
        print("\n" + "=" * 60)
        print("[PASS] All user profile tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        import sys
        sys.exit(1)

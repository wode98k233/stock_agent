"""
Test: Agent 工具函数
验证: agents/plan/analyze.py - is_abnormal_result, error_fingerprint, keyword_overlap
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '..'))

from agents.plan.analyze import is_abnormal_result, error_fingerprint, keyword_overlap


def test_is_abnormal_result():
    print("=" * 60)
    print("TC-AgentUtils-01: is_abnormal_result")
    print("=" * 60)

    assert is_abnormal_result('""') == True, "empty quote"
    assert is_abnormal_result('"code"') == True, "code string"
    assert is_abnormal_result('null') == True, "null"
    assert is_abnormal_result('undefined') == True, "undefined"
    assert is_abnormal_result('Sorry, need more steps') == True, "max iterations"
    assert is_abnormal_result('Agent stopped due to max iterations') == True, "agent stopped"
    assert is_abnormal_result('hi') == True, "too short"
    assert is_abnormal_result('') == True, "empty string"
    assert is_abnormal_result(None) == True, "None"

    normal = "根据分析，今天大盘整体呈现上涨趋势，电池板块表现强劲..."
    assert is_abnormal_result(normal) == False, "normal text"

    print("[OK] is_abnormal_result passed")


def test_error_fingerprint():
    print("\n" + "=" * 60)
    print("TC-AgentUtils-02: error_fingerprint")
    print("=" * 60)

    class MockError(Exception):
        pass

    e1 = ValueError('{"code": "invalid"}')
    fp1 = error_fingerprint(e1)
    print(f"  ValueError with 'code': {fp1}")
    assert "code_related" in fp1, f"expected code_related, got {fp1}"

    e2 = TimeoutError('connection timeout after 30s')
    fp2 = error_fingerprint(e2)
    print(f"  TimeoutError: {fp2}")
    assert "timeout" in fp2

    e3 = RecursionError('maximum recursion depth exceeded')
    fp3 = error_fingerprint(e3)
    print(f"  RecursionError: {fp3}")
    assert "recursion_limit" in fp3

    e4 = ValueError('invalid json: unexpected token')
    fp4 = error_fingerprint(e4)
    print(f"  JSON error: {fp4}")
    assert "json_parse" in fp4

    e5 = MockError('some unknown error')
    fp5 = error_fingerprint(e5)
    print(f"  Unknown error: {fp5}")
    assert "MockError" in fp5

    print("[OK] error_fingerprint passed")


def test_keyword_overlap_identical():
    print("\n" + "=" * 60)
    print("TC-AgentUtils-03: 完全相同字符串 keyword_overlap")
    print("=" * 60)

    sim = keyword_overlap("搜索电池板块新闻", "搜索电池板块新闻")
    print(f"  完全相同: {sim}")
    assert sim == 1.0, f"期望 1.0, 得到 {sim}"
    print("[OK]")


def test_keyword_overlap_empty():
    print("\n" + "=" * 60)
    print("TC-AgentUtils-04: 空字符串处理 keyword_overlap")
    print("=" * 60)

    assert keyword_overlap("", "") == 0.0
    assert keyword_overlap("abc", "") == 0.0
    assert keyword_overlap("", "xyz") == 0.0
    print("[OK] 空字符串返回 0.0")


def test_keyword_overlap_threshold_07():
    print("\n" + "=" * 60)
    print("TC-AgentUtils-05: 目的相似度阈值 0.7 keyword_overlap")
    print("=" * 60)

    pairs = [
        ("搜索电池板块新闻", "搜索电池板块新闻", True),
        ("宁德时代走势分析", "宁德时代走势分析", True),
        ("电池板块资讯", "电池板块资讯", True),
        ("搜索电池新闻", "查询券商板块行情", False),
    ]

    for a, b, expected in pairs:
        sim = keyword_overlap(a, b)
        result = sim > 0.7
        status = "通过" if result == expected else "失败"
        print(f"  [{status}] '{a}' vs '{b}': {sim:.3f} {'>' if result else '<='} 0.7")
        assert result == expected, f"期望 {expected}, 得到 {result} for '{a}' vs '{b}'"

    print("[OK] 阈值 0.7 判断正确")


def test_replan_dedup_logic():
    print("\n" + "=" * 60)
    print("TC-AgentUtils-07: 模拟 Replanner 去重逻辑")
    print("=" * 60)

    completed_purposes = ["搜索电池板块新闻"]
    completed_instructions = ["搜索电池板块新闻"]
    pending_instructions = ["查询券商板块行情"]

    new_step_purpose = "搜索电池板块新闻"
    new_step_instruction = "搜索电池板块新闻"

    is_dup_purpose = any(
        keyword_overlap(new_step_purpose, p) > 0.7
        for p in completed_purposes
    )
    is_dup_instr = any(
        keyword_overlap(new_step_instruction, i) > 0.85
        for i in completed_instructions
    )
    is_dup_pending = any(
        keyword_overlap(new_step_instruction, p) > 0.7
        for p in pending_instructions
    )

    print(f"  目的相似: {is_dup_purpose} (相似度 {keyword_overlap(new_step_purpose, completed_purposes[0]):.3f})")
    print(f"  指令重复: {is_dup_instr}")
    print(f"  已在计划: {is_dup_pending}")

    assert is_dup_purpose == True, "目的应该被识别为相似"
    assert is_dup_instr == True, "指令应该被识别为重复"
    print("[OK] 去重逻辑正确识别重复步骤")


def test_replan_all_unique():
    print("\n" + "=" * 60)
    print("TC-AgentUtils-08: 完全不同的步骤不应被过滤")
    print("=" * 60)

    completed_purposes = ["搜索电池板块资讯"]
    new_step_purpose = "分析券商板块走势"

    sim = keyword_overlap(new_step_purpose, completed_purposes[0])
    is_dup = sim > 0.7

    print(f"  '分析券商板块走势' vs '搜索电池板块资讯': {sim:.3f}")
    assert is_dup == False, "不相关的步骤不应被过滤"
    print("[OK] 不同步骤正常通过")


if __name__ == "__main__":
    try:
        test_is_abnormal_result()
        test_error_fingerprint()
        test_keyword_overlap_identical()
        test_keyword_overlap_empty()
        test_keyword_overlap_threshold_07()
        test_replan_dedup_logic()
        test_replan_all_unique()
        print("\n" + "=" * 60)
        print("[PASS] All agent utils tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

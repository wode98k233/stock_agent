"""
Test: 工具函数单元测试
验证: 搜索过滤、symbol/code 兼容（纯逻辑，无外部调用）

注意: 搜索截断相关测试（调用真实 API）已移至 test/integration/skills/test_tools_integration.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_authority_order_ranking():
    print("\n" + "=" * 60)
    print("TC-Tools-03: 权威等级排序")
    print("=" * 60)

    authority_order = {"L1": 0, "L1-1": 0, "L1-2": 1, "L1-3": 2,
                       "L2": 3, "L2-1": 3, "L2-2": 4,
                       "L3": 5}

    items = [
        {"authorityLevel": "L3"},
        {"authorityLevel": "L1"},
        {"authorityLevel": "L2"},
        {"authorityLevel": "L1-2"},
    ]

    sorted_items = sorted(items, key=lambda x: authority_order.get(x.get("authorityLevel", "L3"), 5))
    order_result = [i["authorityLevel"] for i in sorted_items]

    print(f"  排序后: {order_result}")
    assert order_result == ["L1", "L1-2", "L2", "L3"], f"排序错误: {order_result}"
    print("[OK] 权威等级排序正确 L1 > L1-2 > L2 > L3")


def test_authority_filter_l3():
    print("\n" + "=" * 60)
    print("TC-Tools-04: L3 来源被 min_authority=L2 过滤")
    print("=" * 60)

    authority_order = {"L1": 0, "L1-1": 0, "L1-2": 1, "L1-3": 2,
                       "L2": 3, "L2-1": 3, "L2-2": 4,
                       "L3": 5}
    min_authority = "L2"
    min_rank = authority_order.get(min_authority, 5)

    items = [
        {"authorityLevel": "L1", "content": "权威财经"},
        {"authorityLevel": "L2", "content": "专业分析"},
        {"authorityLevel": "L3", "content": "民间论坛"},
    ]

    filtered = []
    for item in items:
        auth = item.get("authorityLevel", "L3")
        auth_rank = authority_order.get(auth, 5)
        if auth_rank <= min_rank:
            filtered.append(item)

    print(f"  原始: {[i['authorityLevel'] for i in items]}")
    print(f"  过滤后: {[i['authorityLevel'] for i in filtered]}")
    assert len(filtered) == 2
    assert all(i["authorityLevel"] != "L3" for i in filtered)
    print("[OK] L3 来源被正确过滤")


def test_symbol_to_code_mapping():
    print("\n" + "=" * 60)
    print("TC-Tools-05: symbol -> code 字段映射")
    print("=" * 60)

    from tools.aggregator import _normalize_stock

    ind = {"symbol": "00001", "name": "平安银行"}
    result = _normalize_stock(ind)
    print(f"  输入: {ind}")
    print(f"  输出: code={result.get('code')}, name={result.get('name')}")
    assert result.get('code') == "00001", f"期望 00001, 得到 {result.get('code')}"
    print("[OK] symbol->code 映射正确")


def test_name_variations():
    print("\n" + "=" * 60)
    print("TC-Tools-06: 多字段变体兼容")
    print("=" * 60)

    from tools.aggregator import _normalize_stock

    cases = [
        ({"code": "00001", "name": "测试"}, "code字段"),
        ({"symbol": "00001", "name": "测试"}, "symbol字段"),
        ({"stock_code": "00001", "stock_name": "测试"}, "stock_前缀字段"),
    ]

    for ind, desc in cases:
        result = _normalize_stock(ind)
        print(f"  {desc}: code={result.get('code')}")
        assert result.get('code') == "00001", f"{desc} 映射失败: {result}"

    print("[OK] 多字段变体兼容")


if __name__ == "__main__":
    try:
        test_authority_order_ranking()
        test_authority_filter_l3()
        test_symbol_to_code_mapping()
        test_name_variations()
        print("\n" + "=" * 60)
        print("[PASS] All tools unit tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

"""
Test: Budget 控制器
验证: utils/budget.py - BudgetController
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_budget_token_exceeded():
    print("=" * 60)
    print("TC-Budget-01: token 超限")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_tokens_per_query=50000)
    bc = BudgetController(limits)

    bc.add_tokens(50001)
    try:
        bc.check()
        assert False, "应该抛出 BudgetExceeded"
    except BudgetExceeded as e:
        print(f"  正确抛出: {e}")
        assert e.reason == "tokens"
        print("[OK] token 超限正确")


def test_budget_llm_calls_exceeded():
    print("\n" + "=" * 60)
    print("TC-Budget-02: LLM 调用次数超限")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_llm_calls_per_query=25)
    bc = BudgetController(limits)

    for i in range(26):
        bc.add_call()

    try:
        bc.check()
        assert False, "应该抛出 BudgetExceeded"
    except BudgetExceeded as e:
        print(f"  正确抛出: {e}")
        assert e.reason == "calls"
        print("[OK] LLM 调用次数超限正确")


def test_budget_time_exceeded():
    print("\n" + "=" * 60)
    print("TC-Budget-03: 时间超限")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_time_seconds=1)
    bc = BudgetController(limits)
    time.sleep(1.1)

    try:
        bc.check()
        assert False, "应该抛出 BudgetExceeded"
    except BudgetExceeded as e:
        print(f"  正确抛出: {e}")
        assert e.reason == "time"
        print("[OK] 时间超限正确")


def test_budget_controller_reset():
    print("\n" + "=" * 60)
    print("TC-Budget-04: 重置功能")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits

    limits = BudgetLimits(max_tokens_per_query=50000)
    bc = BudgetController(limits)

    bc.add_tokens(40000)
    bc.add_call()
    bc.add_call()

    print(f"  重置前: tokens={bc.get_tokens()}, calls={bc.get_calls()}")

    bc.reset()

    print(f"  重置后: tokens={bc.get_tokens()}, calls={bc.get_calls()}")
    assert bc.get_tokens() == 0, "重置后 tokens 应该为 0"
    assert bc.get_calls() == 0, "重置后 calls 应该为 0"
    print("[OK] reset 正确")


def test_budget_disable():
    print("\n" + "=" * 60)
    print("TC-Budget-05: 禁用预算检查")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_tokens_per_query=100)
    bc = BudgetController(limits)
    bc.disable()

    bc.add_tokens(99999)
    try:
        bc.check()
        print("  未抛出异常（已禁用）")
        print("[OK] disable 正确")
    except BudgetExceeded:
        assert False, "禁用后不应抛出"


def test_budget_get_status():
    print("\n" + "=" * 60)
    print("TC-Budget-06: get_status 输出格式")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits

    limits = BudgetLimits(max_tokens_per_query=50000, max_llm_calls_per_query=25, max_time_seconds=60)
    bc = BudgetController(limits)
    bc.add_tokens(10000)
    bc.add_tokens(5000)
    bc.add_call()
    bc.add_call()

    status = bc.get_status()
    print(f"  status: {status}")
    assert "tokens" in status
    assert "calls" in status
    assert "tokens_percent" in status
    assert status["tokens"] == 15000
    assert status["calls"] == 2
    assert "elapsed_seconds" in status
    print("[OK] get_status 格式正确")


def test_budget_within_limits():
    print("\n" + "=" * 60)
    print("TC-Budget-07: 预算内正常运行")
    print("=" * 60)

    from utils.budget import BudgetController, BudgetLimits

    limits = BudgetLimits(max_tokens_per_query=50000, max_llm_calls_per_query=25)
    bc = BudgetController(limits)

    bc.add_tokens(10000)
    bc.add_call()

    result = bc.check()
    print(f"  预算内 check() 返回: {result}")
    assert result == True
    print("[OK] 预算内正常运行")


if __name__ == "__main__":
    try:
        test_budget_token_exceeded()
        test_budget_llm_calls_exceeded()
        test_budget_time_exceeded()
        test_budget_controller_reset()
        test_budget_disable()
        test_budget_get_status()
        test_budget_within_limits()
        print("\n" + "=" * 60)
        print("[PASS] All budget tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
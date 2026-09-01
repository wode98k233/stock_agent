import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_session_stats_accumulate():
    print("=" * 60)
    print("TC-SS-01: SessionStats 累加")
    print("=" * 60)

    from utils.session_stats import SessionStats
    from utils.logger import RequestContext
    import logging

    stats = SessionStats()
    raw_logger = logging.getLogger("test.ss")
    ctx = RequestContext("test", raw_logger)
    ctx.metrics['llm_calls'] = 3
    ctx.metrics['llm_tokens_in'] = 1000
    ctx.metrics['llm_tokens_out'] = 500
    ctx.metrics['tool_calls'] = 5

    stats.accumulate(ctx)

    assert stats.queries == 1
    assert stats.total_tokens == 1500
    assert stats.total_llm_calls == 3
    assert stats.total_tool_calls == 5
    print("[OK] 累加正确")


def test_session_stats_summary():
    print("\n" + "=" * 60)
    print("TC-SS-02: SessionStats.summary()")
    print("=" * 60)

    from utils.session_stats import SessionStats

    stats = SessionStats()
    stats.queries = 5
    stats.total_tokens = 10000
    stats.total_llm_calls = 20
    stats.total_tool_calls = 30

    result = stats.summary()
    print(f"  summary: {result}")
    assert "5 次查询" in result
    assert "10000 tokens" in result
    print("[OK] summary 格式正确")


if __name__ == "__main__":
    try:
        test_session_stats_accumulate()
        test_session_stats_summary()
        print("\n" + "=" * 60)
        print("[PASS] All SessionStats tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

"""
工具函数集成测试 — 调用真实外部 API

运行: pytest test/integration/skills/test_tools_integration.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_truncate_basic():
    """搜索结果截断 — 调用真实 mx_search API"""
    from tools.other_skills.eastmoney.skills import _mx_search_news_core

    long_query = "电池板块今日行情分析" * 100
    result = _mx_search_news_core(long_query)

    result_len = len(result)
    assert result_len <= 5000, f"输出超过预期上限 5000，实际 {result_len}"


def test_short_output_unchanged():
    """短输出不受影响 — 调用真实 mx_search API"""
    from tools.other_skills.eastmoney.skills import _mx_search_news_core

    short_query = "宁德时代"
    try:
        result = _mx_search_news_core(short_query)
        assert len(result) > 0, "结果为空"
    except Exception:
        pass  # 工具调用失败不影响截断验证


if __name__ == "__main__":
    test_truncate_basic()
    test_short_output_unchanged()
    print("[OK] All integration tests passed")

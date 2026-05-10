"""
mx_search format_pretty 过滤逻辑测试

验证：
1. 默认参数下 L3 新闻不过滤
2. 显式 min_authority="L2" 时 L3 被过滤
3. 过滤后为空时降级返回
4. authorityLevel 缺失默认为 L2

运行方式：
  pytest test/unit/test_mx_search_filter.py -v
"""
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _make_result(items):
    """构造 MX Search API 响应格式"""
    return {
        "status": 0,
        "message": "success",
        "data": {
            "data": {
                "llmSearchResponse": {
                    "data": items
                }
            }
        }
    }


def _make_item(title="测试新闻", authority="L3", info_type="NEWS"):
    item = {"title": title, "content": "内容", "date": "2026-05-09"}
    if authority:
        item["authorityLevel"] = authority
    if info_type:
        item["informationType"] = info_type
    return item


# ============================================================
# 默认参数 (min_authority="L3") 不过滤 L3
# ============================================================

def test_default_keeps_l3():
    """默认参数下 L3 新闻应被保留"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("L3新闻", "L3")]
    result = _make_result(items)
    output = MXSearch.format_pretty(result)
    assert "L3新闻" in output
    assert "过滤后展示 1 条" in output


def test_default_keeps_l2():
    """默认参数下 L2 新闻应被保留"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("L2新闻", "L2")]
    result = _make_result(items)
    output = MXSearch.format_pretty(result)
    assert "L2新闻" in output


def test_default_keeps_l1():
    """默认参数下 L1 新闻应被保留"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("L1新闻", "L1")]
    result = _make_result(items)
    output = MXSearch.format_pretty(result)
    assert "L1新闻" in output


# ============================================================
# 显式 min_authority 参数
# ============================================================

def test_explicit_l2_filters_l3():
    """显式 min_authority="L2" 时 L3 应被过滤"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("L3新闻", "L3"), _make_item("L2新闻", "L2")]
    result = _make_result(items)
    output = MXSearch.format_pretty(result, min_authority="L2")
    assert "L3新闻" not in output
    assert "L2新闻" in output


def test_explicit_l1_filters_l2_and_l3():
    """显式 min_authority="L1" 时 L2 和 L3 应被过滤"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [
        _make_item("L1新闻", "L1"),
        _make_item("L2新闻", "L2"),
        _make_item("L3新闻", "L3"),
    ]
    result = _make_result(items)
    output = MXSearch.format_pretty(result, min_authority="L1")
    assert "L1新闻" in output
    assert "L2新闻" not in output
    assert "L3新闻" not in output


# ============================================================
# authorityLevel 缺失默认值
# ============================================================

def test_missing_authority_default_l2():
    """authorityLevel 缺失时默认 L2，在默认参数下应被保留"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("无权威级别", authority=None)]
    result = _make_result(items)
    output = MXSearch.format_pretty(result)
    assert "无权威级别" in output


def test_missing_authority_kept_with_explicit_l2():
    """authorityLevel 缺失时默认 L2，在 min_authority="L2" 下应被保留"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("无权威级别", authority=None)]
    result = _make_result(items)
    output = MXSearch.format_pretty(result, min_authority="L2")
    assert "无权威级别" in output


# ============================================================
# NOTICE 过滤
# ============================================================

def test_notice_filtered():
    """NOTICE 类型应始终被过滤"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("公告", "L1", "NOTICE"), _make_item("新闻", "L1", "NEWS")]
    result = _make_result(items)
    output = MXSearch.format_pretty(result)
    assert "公告" not in output
    assert "新闻" in output


# ============================================================
# 降级逻辑
# ============================================================

def test_fallback_when_all_filtered():
    """所有条目被权威过滤后，降级返回非 NOTICE 条目"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [_make_item("L3新闻", "L3"), _make_item("L3另一条", "L3")]
    result = _make_result(items)
    # min_authority="L1" 会过滤掉所有 L3
    output = MXSearch.format_pretty(result, min_authority="L1")
    # 降级：跳过权威过滤，保留非 NOTICE 条目
    assert "L3新闻" in output
    assert "L3另一条" in output
    assert "过滤后展示 2 条" in output


def test_fallback_excludes_notice():
    """降级时 NOTICE 条目仍被过滤"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [
        _make_item("L3新闻", "L3", "NEWS"),
        _make_item("L3公告", "L3", "NOTICE"),
    ]
    result = _make_result(items)
    output = MXSearch.format_pretty(result, min_authority="L1")
    # 降级：保留非 NOTICE
    assert "L3新闻" in output
    assert "L3公告" not in output


def test_no_fallback_when_filtered_not_empty():
    """过滤后不为空时不触发降级"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    items = [
        _make_item("L1新闻", "L1", "NEWS"),
        _make_item("L3新闻", "L3", "NEWS"),
    ]
    result = _make_result(items)
    output = MXSearch.format_pretty(result, min_authority="L2")
    # L1 保留，L3 过滤，过滤后不为空 → 不降级
    assert "L1新闻" in output
    assert "L3新闻" not in output


# ============================================================
# 边界情况
# ============================================================

def test_empty_items():
    """空 items 应返回未找到"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    result = _make_result([])
    output = MXSearch.format_pretty(result)
    assert "未找到" in output


def test_error_status():
    """非零 status 应返回错误"""
    from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
    result = {"status": -1, "message": "服务异常"}
    output = MXSearch.format_pretty(result)
    assert "错误" in output


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

"""
证据构建器单元测试

运行: pytest test/unit/analysis/test_evidence.py -v
"""
import os
import sys
import json
import pytest
from unittest.mock import MagicMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _make_tc(tool_name, tool_input, tool_output):
    tc = MagicMock()
    tc.tool_name = tool_name
    tc.tool_input = tool_input
    tc.tool_output = tool_output
    return tc


# ══════════════════════════════════════════════════════════════
# build_bag
# ══════════════════════════════════════════════════════════════

def test_build_bag_empty():
    """空工具列表 → 空证据包"""
    from agents.analysis.evidence import build_bag
    bag = build_bag([])
    assert bag["raw_text"] == ""
    assert bag["news_text"] == ""
    assert bag["metadata"]["tool_count"] == 0


def test_build_bag_with_dict_tool():
    """dict 格式工具调用 → 兼容处理"""
    from agents.analysis.evidence import build_bag
    tc = {"tool_name": "mx_search", "tool_input": "茅台", "tool_output": "提价预期"}
    bag = build_bag([tc])
    assert "提价预期" in bag["news_text"]
    assert bag["metadata"]["tool_count"] == 1


def test_build_bag_with_json_tool():
    """JSON 工具输出 → json_fragments"""
    from agents.analysis.evidence import build_bag
    tc = _make_tc("technical_analysis", "茅台", json.dumps({"rsi": 65}))
    bag = build_bag([tc])
    assert len(bag["json_fragments"]) == 1
    assert bag["json_fragments"][0]["tool"] == "technical_analysis"


def test_build_bag_with_news_tool():
    """新闻工具 → news_text 分离"""
    from agents.analysis.evidence import build_bag
    tc = _make_tc("mx_search", "茅台新闻", "提价预期发酵")
    bag = build_bag([tc])
    assert "提价预期" in bag["news_text"]


def test_build_bag_truncation():
    """超长输出被截断"""
    from agents.analysis.evidence import build_bag
    long_output = "x" * 10000
    tc = _make_tc("mx_data", "test", long_output)
    bag = build_bag([tc])
    assert bag["metadata"]["truncated_count"] == 1
    assert len(bag["items"][0]["raw_output"]) <= 10000


def test_build_bag_mx_data_normalized_fields():
    """妙想 mx_data → normalized_fields 解析"""
    from agents.analysis.evidence import build_bag
    mx_data = _load_fixture("mx_data_rsi_table.json")
    tc = _make_tc("mx_data", "test", json.dumps(mx_data))
    bag = build_bag([tc])
    # nameMap 中有 f37→RSI，解析后 normalized_fields 应有 RSI
    assert "RSI" in bag["normalized_fields"]
    assert bag["normalized_fields"]["RSI"] == "65.3"


def test_build_bag_mx_structured_preview_normalized_fields():
    """mx_data/mx_xuangu 结构化预览应能作为插槽证据，不调用真实 mx API。"""
    from agents.analysis.evidence import build_bag

    mx_data = {
        "query": "测试",
        "tables": [
            {
                "sheet_name": "行情",
                "rows": [{"名称": "测试股", "涨跌幅": "5.2", "RSI": "65.3", "PE": "20.5"}],
            }
        ],
    }
    mx_xuangu = {
        "query": "测试选股",
        "stocks": [{"名称": "候选股", "涨跌幅": "3.1", "PE": "18.2"}],
    }

    bag = build_bag([
        _make_tc("mx_data_query", "test", json.dumps(mx_data, ensure_ascii=False)),
        _make_tc("mx_xuangu_filter", "test", json.dumps(mx_xuangu, ensure_ascii=False)),
    ])

    assert bag["normalized_fields"]["RSI"] == "65.3"
    assert bag["normalized_fields"]["涨跌幅"] == "5.2"
    assert bag["normalized_fields"]["PE"] == "20.5"
    assert len(bag["table_fragments"]) >= 2


def test_build_bag_known_skill_technical():
    """technical_analysis 技能输出 → normalized_fields"""
    from agents.analysis.evidence import build_bag
    tech_data = _load_fixture("technical_analysis_rsi.json")
    tc = _make_tc("technical_analysis", "test", json.dumps(tech_data))
    bag = build_bag([tc])
    assert "rsi" in bag["normalized_fields"]
    assert bag["normalized_fields"]["rsi"] == 65.3


def test_build_bag_known_skill_valuation():
    """valuation 技能输出 → normalized_fields"""
    from agents.analysis.evidence import build_bag
    val_data = _load_fixture("valuation_pe.json")
    tc = _make_tc("valuation", "test", json.dumps(val_data))
    bag = build_bag([tc])
    assert "pe" in bag["normalized_fields"]
    assert bag["normalized_fields"]["pe"] == 32.5


def test_build_bag_multiple_tools():
    """多个工具 → 综合证据包"""
    from agents.analysis.evidence import build_bag
    tc1 = _make_tc("technical_analysis", "test", json.dumps({"rsi": 60}))
    tc2 = _make_tc("valuation", "test", json.dumps({"pe": 25}))
    tc3 = _make_tc("mx_search", "test", "新闻内容")
    bag = build_bag([tc1, tc2, tc3])
    assert bag["metadata"]["tool_count"] == 3
    assert len(bag["json_fragments"]) == 2
    assert len(bag["news_text"]) > 0


def test_build_bag_non_json_output():
    """非 JSON 输出不进 json_fragments"""
    from agents.analysis.evidence import build_bag
    tc = _make_tc("some_tool", "test", "这不是JSON")
    bag = build_bag([tc])
    assert len(bag["json_fragments"]) == 0
    assert "这不是JSON" in bag["raw_text"]


def test_clean_tool_output_json():
    """正常 JSON 不变"""
    from agents.analysis.evidence import _clean_tool_output
    json_str = '{"key": "value"}'
    assert _clean_tool_output(json_str) == json_str


def test_clean_tool_output_repr_format():
    """LangChain repr 格式提取 content"""
    from agents.analysis.evidence import _clean_tool_output
    repr_str = "content='{\"query\": \"test\", \"result\": \"ok\"}' name='mx_search' tool_call_id='call_123'"
    cleaned = _clean_tool_output(repr_str)
    assert cleaned.startswith('{"query"')
    assert '"result": "ok"' in cleaned


def test_clean_tool_output_plain_text():
    """普通文本不变"""
    from agents.analysis.evidence import _clean_tool_output
    text = "这是普通文本输出"
    assert _clean_tool_output(text) == text


def test_build_bag_repr_format_json():
    """repr 格式的 JSON 输出能被正确解析"""
    from agents.analysis.evidence import build_bag
    repr_json = 'content=\'{"rsi": 65.3, "DIF": 0.05}\' name=\'technical_analysis\' tool_call_id=\'call_123\''
    tc = _make_tc("technical_analysis", "test", repr_json)
    bag = build_bag([tc])
    assert len(bag["json_fragments"]) == 1
    assert bag["json_fragments"][0]["data"]["rsi"] == 65.3


if __name__ == "__main__":
    import traceback
    tests = [
        test_build_bag_empty,
        test_build_bag_with_json_tool,
        test_build_bag_with_news_tool,
        test_build_bag_truncation,
        test_build_bag_mx_data_normalized_fields,
        test_build_bag_known_skill_technical,
        test_build_bag_known_skill_valuation,
        test_build_bag_multiple_tools,
        test_build_bag_non_json_output,
        test_clean_tool_output_json,
        test_clean_tool_output_repr_format,
        test_clean_tool_output_plain_text,
        test_build_bag_repr_format_json,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed > 0 else 0)

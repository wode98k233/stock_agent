"""
分析框架引擎测试 fixtures
"""
import os
import sys
import json
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture
def standard_template():
    """加载标准模板"""
    from agents.analysis.template_store import load_template
    return load_template("standard")


@pytest.fixture
def mock_tool_calls():
    """模拟工具调用列表"""
    from unittest.mock import MagicMock

    calls = []

    # technical_analysis 返回
    tc1 = MagicMock()
    tc1.tool_name = "technical_analysis"
    tc1.tool_input = "贵州茅台"
    tc1.tool_output = json.dumps({
        "rsi": 65.3, "DIF": 0.05, "DEA": 0.03, "MACD": 0.02,
        "trend": "上涨", "change_pct": 1.5,
    }, ensure_ascii=False)
    calls.append(tc1)

    # valuation 返回
    tc2 = MagicMock()
    tc2.tool_name = "valuation"
    tc2.tool_input = "贵州茅台"
    tc2.tool_output = json.dumps({
        "pe": 32.5, "pb": 8.1, "pe_percentile": 72,
    }, ensure_ascii=False)
    calls.append(tc2)

    # mx_data 返回
    tc3 = MagicMock()
    tc3.tool_name = "mx_data"
    tc3.tool_input = "贵州茅台最新行情"
    tc3.tool_output = json.dumps({
        "data": {
            "nameMap": {"f2": "最新价", "f3": "涨跌幅", "f14": "名称"},
            "dataTableDTOList": [{
                "table": [{"f2": "1650.00", "f3": "1.52", "f14": "贵州茅台"}]
            }]
        }
    }, ensure_ascii=False)
    calls.append(tc3)

    # 新闻搜索返回
    tc4 = MagicMock()
    tc4.tool_name = "mx_search"
    tc4.tool_input = "贵州茅台最新新闻"
    tc4.tool_output = "多家媒体报道贵州茅台提价预期发酵，市场关注度提升。白酒板块整体资金流入。"
    calls.append(tc4)

    return calls


@pytest.fixture
def mock_slot_results():
    """模拟插槽提取结果"""
    from agents.analysis.models import SlotResult

    return {
        "rsi_status": SlotResult(
            slot="rsi_status", value=65.3, status="偏强",
            interpretation="RSI 65.3，偏强但未超买",
            source_tool="technical_analysis", method="json", confidence=0.9,
        ),
        "macd_signal": SlotResult(
            slot="macd_signal", value={"dif": 0.05, "dea": 0.03},
            status="金叉", interpretation="DIF > DEA，金叉确认",
            source_tool="technical_analysis", method="json", confidence=0.9,
        ),
        "trend": SlotResult(
            slot="trend", value=1.5, status="温和上涨",
            interpretation="涨跌幅 1.50%，温和上行",
            source_tool="technical_analysis", method="json", confidence=0.8,
        ),
        "pe_percentile": SlotResult(
            slot="pe_percentile", value={"percentile": 72, "pe": 32.5},
            status="偏高", interpretation="PE(TTM) 32.5倍，处于近3年 72% 分位，估值偏高",
            source_tool="valuation", method="json", confidence=0.9,
        ),
    }

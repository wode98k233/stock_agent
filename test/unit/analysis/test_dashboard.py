"""
仪表盘模块单元测试

覆盖：
1. Schema 校验 (T17-T23)
2. 生成器逻辑 (T07-T16)
3. 配置开关 (T41-T44)
4. CLI 格式化 (T37-T40)

运行方式：
  pytest test/unit/test_dashboard.py -v
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest
from pydantic import ValidationError

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agents.analysis.dashboard_schema import DashboardData, DashboardCheckItem, DashboardRiskItem
from agents.analysis.dashboard_config import get_dashboard_config, get_dashboard_category
from agents.analysis.dashboard_generator import (
    generate,
    _calc_sentiment_score,
    _extract_decision_type,
    _extract_price_levels,
    _truncate_report,
    _build_fallback,
)
from agents.analysis.dashboard_cli import format_dashboard_cli
from config import Config
from utils.budget import BudgetExceeded


# ============================================================
# 辅助函数
# ============================================================

def _make_valid_data(**overrides) -> dict:
    data = {
        "core_verdict": "综合分析建议买入",
        "decision_type": "buy",
        "confidence_level": 0.7,
        "sentiment_score": 65,
        "trend_prediction": "bullish",
        "quality_tag": "完整分析",
        "checklist": [
            {"dimension": "技术面", "status": "positive", "detail": "MACD金叉，趋势向上"},
        ],
        "risk_priority": [
            {"level": "medium", "category": "市场风险", "detail": "大盘震荡", "action": "控制仓位"},
        ],
        "key_points": ["突破前高", "量能放大"],
        "next_watch": ["关注5日均线支撑"],
    }
    data.update(overrides)
    return data


def _make_slot_results_with_signals() -> dict:
    return {
        "涨跌幅": "上涨3.5%，突破前高",
        "主力净流入": "净流入1.2亿，多头增仓",
        "RSI": "RSI=65，偏多",
        "MACD": "MACD金叉，看多信号",
        "换手率": "换手率5.2%，放量上涨",
    }


# ============================================================
# 1. Schema 校验测试 (T17-T23)
# ============================================================

def test_schema_valid_data():
    """T17: 合法 DashboardData 通过校验"""
    data = DashboardData(**_make_valid_data())
    assert data.decision_type == "buy"
    assert data.confidence_level == 0.7
    assert data.sentiment_score == 65
    assert data.quality_tag == "完整分析"


def test_schema_invalid_decision_type():
    """T18: decision_type="strong_buy" 抛 ValidationError"""
    with pytest.raises(ValidationError):
        DashboardData(**_make_valid_data(decision_type="strong_buy"))


def test_schema_invalid_confidence_level():
    """T19: confidence_level=1.5 抛 ValidationError"""
    with pytest.raises(ValidationError):
        DashboardData(**_make_valid_data(confidence_level=1.5))


def test_schema_invalid_sentiment_score():
    """T20: sentiment_score=-5 或 150 抛 ValidationError"""
    with pytest.raises(ValidationError):
        DashboardData(**_make_valid_data(sentiment_score=-5))
    with pytest.raises(ValidationError):
        DashboardData(**_make_valid_data(sentiment_score=150))


def test_schema_empty_checklist():
    """T21: checklist=[] 通过校验"""
    data = DashboardData(**_make_valid_data(checklist=[]))
    assert data.checklist == []


def test_schema_only_common_fields():
    """T22: 扩展字段全为 None 通过校验"""
    data = DashboardData(**_make_valid_data())
    assert data.price_levels is None
    assert data.split_advice is None
    assert data.position_guidance is None
    assert data.market_temperature is None
    assert data.sector_rotation is None
    assert data.sector_stage is None
    assert data.leading_stocks is None
    assert data.portfolio_action is None
    assert data.action_items is None
    assert data.event_impact is None
    assert data.falsification_signal is None


def test_schema_stock_extended_fields():
    """T23: price_levels + split_advice + position_guidance 填充"""
    data = DashboardData(**_make_valid_data(
        price_levels={"支撑": 10.5, "压力": 12.0, "止损": 10.0, "目标": 13.0},
        split_advice={"no_position": "逢低买入", "has_position": "持有待涨"},
        position_guidance={"suggested_position": "3成", "max_position": "5成"},
    ))
    assert data.price_levels["支撑"] == 10.5
    assert data.split_advice["no_position"] == "逢低买入"
    assert data.position_guidance["suggested_position"] == "3成"


# ============================================================
# 2. 生成器逻辑测试 (T07-T16)
# ============================================================

@pytest.mark.parametrize(("text", "expected"), [
    ("20 日均线支撑位在 28.50 元", {"支撑": 28.5}),
    ("2026-07-17 压力位在 32 元", {"压力": 32.0}),
    ("28.5 元为支撑位", {"支撑": 28.5}),
])
def test_extract_price_levels_uses_keyword_adjacent_price(text, expected):
    assert _extract_price_levels({"技术分析": text}) == expected


@pytest.mark.parametrize("text", [
    "支撑位在 20 日均线附近",
    "支撑区间 28-30 元",
    "压力位对应涨幅 5%",
    "支撑位参考股票代码 600519",
    "支撑位 28.5 元或 29.2 元",
])
def test_extract_price_levels_rejects_non_price_or_ambiguous_values(text):
    assert _extract_price_levels({"技术分析": text}) is None

def test_generator_llm_success():
    """T07: mock LLM 返回合法 JSON，返回 DashboardData，quality_tag="完整分析" """
    llm_response = _make_valid_data()

    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        return llm_response

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        result = asyncio.run(generate(
            report_content="分析报告内容",
            slot_results={},
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    assert result is not None
    assert isinstance(result, DashboardData)
    assert result.quality_tag == "完整分析"


def test_generator_llm_invalid_json():
    """T08: mock LLM 返回非法字符串，降级为规则预提取"""
    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        return "this is not a valid json"

    slot_results = _make_slot_results_with_signals()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results=slot_results,
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    assert result is not None
    assert result.quality_tag == "快速概览"


def test_generator_llm_partial_fields():
    """T09: mock LLM 返回部分字段，缺失字段补 null"""
    partial_data = {
        "core_verdict": "建议买入",
        "decision_type": "buy",
        "confidence_level": 0.6,
        "sentiment_score": 55,
        "trend_prediction": "bullish",
        "quality_tag": "完整分析",
        "checklist": [],
        "risk_priority": [],
        "key_points": ["突破前高"],
        "next_watch": [],
    }

    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        return partial_data

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results={},
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    assert result is not None
    assert result.quality_tag == "完整分析"
    assert result.price_levels is None
    assert result.split_advice is None
    assert result.position_guidance is None


def test_generator_llm_timeout():
    """T10: mock LLM 超时，降级为规则预提取"""
    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        raise TimeoutError("LLM 请求超时")

    slot_results = _make_slot_results_with_signals()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results=slot_results,
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    assert result is not None
    assert result.quality_tag == "快速概览"


def test_generator_budget_exhausted():
    """T11: DASHBOARD_MAX_LLM_TOKENS=0，仅规则预提取"""
    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        raise BudgetExceeded("tokens")

    slot_results = _make_slot_results_with_signals()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch.object(Config, "DASHBOARD_MAX_LLM_TOKENS", 0), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results=slot_results,
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    assert result is not None
    assert result.quality_tag == "快速概览"


def test_generator_rule_extract_success():
    """T12: slot_results 包含涨跌幅等，sentiment_score 在 0-100"""
    slot_results = _make_slot_results_with_signals()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", False):
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results=slot_results,
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    assert result is not None
    assert 0 <= result.sentiment_score <= 100


def test_generator_rule_extract_no_data():
    """T13: slot_results 为空，fallback 返回最小化仪表盘"""
    with patch.object(Config, "DASHBOARD_LLM_ENABLED", False):
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results={},
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    # LLM 关闭且无规则数据时，fallback 仍返回最小化仪表盘
    assert result is not None
    assert result.decision_type == "hold"
    assert result.quality_tag == "快速概览"
    assert result.sentiment_score == 50


def test_generator_short_report():
    """T14: 报告全文 ≤ 2000 token，LLM 输入为全文"""
    short_report = "短期看多信号明显" * 100

    captured_messages = []

    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        captured_messages.extend(messages)
        return _make_valid_data()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        asyncio.run(generate(
            report_content=short_report,
            slot_results={},
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    user_msg = captured_messages[1][1]
    assert short_report in user_msg
    assert "\n...\n" not in user_msg


def test_generator_long_report():
    """T15: 报告全文 > 2000 token，LLM 输入为截断版本"""
    long_report = "这是一段详细的分析报告内容，包含多维度数据解读。" * 2000

    captured_messages = []

    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        captured_messages.extend(messages)
        return _make_valid_data()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        asyncio.run(generate(
            report_content=long_report,
            slot_results={},
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    user_msg = captured_messages[1][1]
    assert "\n...\n" in user_msg
    assert len(user_msg) < len(long_report)


def test_generator_llm_disabled():
    """T16: DASHBOARD_LLM_ENABLED=false，仅规则预提取"""
    slot_results = _make_slot_results_with_signals()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", False), \
         patch("agents.analysis.dashboard_generator._call_llm") as mock_llm:
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results=slot_results,
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    mock_llm.assert_not_called()
    assert result is not None
    assert result.quality_tag == "快速概览"


# ============================================================
# 3. 配置开关测试 (T41-T44)
# ============================================================

def test_config_dashboard_enabled():
    """T41: DASHBOARD_ENABLED=true"""
    with patch.object(Config, "DASHBOARD_ENABLED", True):
        assert Config.DASHBOARD_ENABLED is True


def test_config_dashboard_disabled():
    """T42: DASHBOARD_ENABLED=false"""
    with patch.object(Config, "DASHBOARD_ENABLED", False):
        assert Config.DASHBOARD_ENABLED is False


def test_config_llm_disabled():
    """T43: DASHBOARD_LLM_ENABLED=false 时 generate 不调用 LLM"""
    slot_results = _make_slot_results_with_signals()

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", False), \
         patch("agents.analysis.dashboard_generator._call_llm") as mock_llm:
        result = asyncio.run(generate(
            report_content="分析报告",
            slot_results=slot_results,
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
        ))

    mock_llm.assert_not_called()
    assert result is not None


def test_unknown_template_defaults_to_stock():
    """T44: 未配置模板默认使用 stock 类别"""
    category = get_dashboard_category("unknown_template")
    assert category is not None
    assert category.scenario_tag == "个股决策仪表盘"


def test_sector_scenes_have_correct_category():
    """板块类模板应映射到 sector 类别"""
    for template_id in ("sector_timing", "sector_landscape", "theme_trading", "industry_chain"):
        category = get_dashboard_category(template_id)
        assert category is not None
        assert category.scenario_tag == "板块决策仪表盘"


def test_market_scenes_have_correct_category():
    """大盘类模板应映射到 market 类别"""
    category = get_dashboard_category("market_daily")
    assert category is not None
    assert category.scenario_tag == "市场决策仪表盘"


def test_macro_scene_has_correct_category():
    """宏观日报应映射到 macro 类别（2026-09-06 从 market 拆分独立宏观仪表盘）"""
    category = get_dashboard_category("macro_daily")
    assert category is not None
    assert category.scenario_tag == "宏观仪表盘"
    assert "macro_snapshot" in category.extended_fields


def test_hot_events_scenes_have_correct_category():
    """热点事件模板应映射到 hot_events 类别"""
    category = get_dashboard_category("event_impact")
    assert category is not None
    assert category.scenario_tag == "热点事件仪表盘"


def test_stock_scenes_have_correct_category():
    """个股类模板应映射到 stock 类别"""
    for template_id in ("stock_deep_dive", "trend_following", "volume_price_alert"):
        category = get_dashboard_category(template_id)
        assert category is not None
        assert category.scenario_tag == "个股决策仪表盘"


def test_all_enabled_templates_have_dashboard_category():
    """所有启用报告模板都应有仪表盘类别"""
    index_path = Path(_project_root) / "agents" / "report_templates" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    enabled_templates = [
        template_id
        for template_id, item in index["templates"].items()
        if item.get("enabled", False)
    ]

    missing = [
        template_id
        for template_id in enabled_templates
        if get_dashboard_category(template_id) is None
    ]

    assert missing == []


def test_dashboard_node_handles_dividend_screening_template():
    """红利模板命中后应追加仪表盘内容"""
    from agents.analysis.dashboard_node import dashboard_node

    dashboard_data = DashboardData(**_make_valid_data(
        core_verdict="dividend dashboard attached",
        key_points=["dividend"],
        next_watch=["watch"],
    ))

    async def _fake_generate(**kwargs):
        return dashboard_data

    state = {
        "response": "report body",
        "template_id": "dividend_screening",
        "input": "dividend sector timing",
    }

    with patch.object(Config, "DASHBOARD_ENABLED", True), \
         patch("agents.analysis.dashboard_node.dashboard_generate", side_effect=_fake_generate):
        result = asyncio.run(dashboard_node(state))

    assert "response" in result
    assert result["response"].startswith("report body")
    assert len(result["response"]) > len("report body")


def test_dashboard_node_uses_react_exec_state_tool_calls():
    """React 模式下应从 exec_state.tool_calls 提取仪表盘证据"""
    from agents.analysis.dashboard_node import dashboard_node
    from agents.executor_callbacks import ExecutionState, ToolCall

    exec_state = ExecutionState()
    exec_state.tool_calls.append(ToolCall(
        tool_name="mx_data_query",
        tool_input="{}",
        tool_output="涨跌幅 上涨3.5%，MACD金叉，主力净流入1.2亿",
    ))

    dashboard_data = DashboardData(**_make_valid_data())
    captured = {}

    async def _fake_generate(**kwargs):
        captured.update(kwargs)
        return dashboard_data

    state = {
        "final_result": "report body",
        "selected_template_id": "market_daily",
        "user_input": "生成大盘总结",
        "exec_state": exec_state,
    }

    with patch.object(Config, "DASHBOARD_ENABLED", True), \
         patch("agents.analysis.dashboard_node.dashboard_generate", side_effect=_fake_generate):
        result = asyncio.run(dashboard_node(state))

    assert "final_result" in result
    assert result["final_result"].startswith("report body")
    assert captured["slot_results"]["mx_data_query"] == exec_state.tool_calls[0].tool_output


def test_generator_coerces_string_extended_fields():
    """LLM 把 dict 扩展字段写成字符串时不应导致仪表盘整体丢失"""
    llm_response = _make_valid_data(position_guidance="权益总仓位建议：半仓")

    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        return llm_response

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        result = asyncio.run(generate(
            report_content="市场报告正文",
            slot_results={},
            template_id="market_daily",
            user_input="生成大盘总结",
        ))

    assert result is not None
    assert result.position_guidance == {"summary": "权益总仓位建议：半仓"}


def test_normalize_nulls_all_zero_breadth():
    """advance/decline 同为 0（占位符）应置 null，避免前端把缺失当真实数据。"""
    from agents.analysis.dashboard_generator import _normalize_llm_payload

    out = _normalize_llm_payload(
        {"market_breadth": {"advance": 0, "decline": 0, "limit_up": 30}})
    assert out["market_breadth"]["advance"] is None
    assert out["market_breadth"]["decline"] is None
    assert out["market_breadth"]["limit_up"] == 30

    keep = _normalize_llm_payload({"market_breadth": {"advance": 1200, "decline": 3000}})
    assert keep["market_breadth"] == {"advance": 1200, "decline": 3000}


# ============================================================
# 4. CLI 格式化测试 (T37-T40)
# ============================================================

def test_cli_format_complete():
    """T37: 完整 DashboardData 输出 ANSI 字符串"""
    data = DashboardData(**_make_valid_data(
        price_levels={"support": 10.5, "resistance": 12.0, "stop_loss": 10.0, "target": 13.0},
        split_advice={"no_position": "逢低买入", "has_position": "持有待涨"},
    ))
    output = format_dashboard_cli(data)
    assert isinstance(output, str)
    assert "\033[" in output
    assert "决策仪表盘" in output
    assert "BUY" in output
    assert "买卖点位" in output
    assert "操作建议" in output


def test_cli_no_web_marker():
    """T38: 输出不含 @@DASHBOARD_START@@"""
    data = DashboardData(**_make_valid_data())
    output = format_dashboard_cli(data)
    assert "@@DASHBOARD_START@@" not in output
    assert "@@DASHBOARD_END@@" not in output


def test_cli_no_price_levels():
    """T39: price_levels=None 时无买卖点位表格"""
    data = DashboardData(**_make_valid_data())
    output = format_dashboard_cli(data)
    assert "买卖点位" not in output


def test_cli_at_end():
    """T40: 格式化输出在报告末尾"""
    data = DashboardData(**_make_valid_data())
    dashboard_str = format_dashboard_cli(data)
    report = "这是分析报告正文内容。"
    combined = report + dashboard_str
    assert combined.index("决策仪表盘") > len(report) - 1


def test_tool_items_keeps_duplicates_in_order():
    """有序调用列表：重复调用（如 get_position_detail×2）必须都保留。"""
    from agents.analysis.dashboard_generator import _tool_items

    calls = [
        {"name": "get_position_detail", "tool_input": "a", "tool_output": "A" * 30},
        {"name": "get_positions", "tool_output": "B" * 50},
        {"name": "get_position_detail", "tool_input": "c", "tool_output": "C" * 40},
    ]
    items = _tool_items(calls, {})
    assert [i["name"] for i in items] == ["get_position_detail", "get_positions", "get_position_detail"]
    assert items[0]["args"] == "a" and items[2]["args"] == "c"


def test_tool_items_falls_back_to_slot_results():
    from agents.analysis.dashboard_generator import _tool_items

    items = _tool_items(None, {"get_positions": "x" * 10})
    assert items == [{"name": "get_positions", "args": "", "output": "x" * 10}]


def test_build_tool_index_shape():
    """索引含 名称/参数/长度/预览；长度按压缩后（值本身）计算。"""
    from agents.analysis.dashboard_generator import _tool_items, _build_tool_index

    items = _tool_items([
        {"name": "get_positions", "tool_input": "{}", "tool_output": "A" * 50},
        {"name": "get_pnl_summary", "tool_output": "B" * 30},
    ], {})
    idx = _build_tool_index(items)
    assert "get_positions" in idx and "长度:50" in idx and "参数:{}" in idx
    assert "get_pnl_summary" in idx and "长度:30" in idx


def test_build_tool_evidence_only_selected():
    """下钻只带被点名的工具；未选中的不进 prompt。"""
    from agents.analysis.dashboard_generator import _tool_items, _build_tool_evidence

    items = _tool_items([{"name": "get_positions", "tool_output": "P" * 4000},
                         {"name": "get_pnl_summary", "tool_output": "Q" * 50}], {})
    ev = _build_tool_evidence(items, selected_names=["get_pnl_summary"])
    assert "get_pnl_summary" in ev and "get_positions" not in ev

    # 点名后单工具不被截（per_tool_chars 足够大）
    ev2 = _build_tool_evidence(items, selected_names=["get_positions"], per_tool_chars=5000)
    assert "P" * 4000 in ev2

    # 空结果跳过
    ev3 = _build_tool_evidence(_tool_items(None, {"empty": ""}), per_tool_chars=100)
    assert "empty" not in ev3


def test_parse_need_tools():
    from agents.analysis.dashboard_generator import _parse_need_tools

    assert _parse_need_tools({"need_tools": ["a", "a", " b ", ""]}) == ["a", "b"]
    assert _parse_need_tools({"need_tools": "a"}) == ["a"]
    assert _parse_need_tools({"core_verdict": "x"}) == []
    assert _parse_need_tools(None) == []


def test_build_prompt_phase1_index_and_detail_request():
    from agents.analysis.dashboard_generator import _build_prompt

    msgs = _build_prompt("报告正文", {}, "问题",
                         tool_index="- get_positions | 长度:10", allow_detail_request=True)
    assert "可下钻" in msgs[0][1]
    assert "工具结果索引" in msgs[1][1] and "need_tools" in msgs[1][1]


def test_build_prompt_phase2_evidence_no_detail_request():
    from agents.analysis.dashboard_generator import _build_prompt

    msgs = _build_prompt("报告正文", {}, "问题", tool_evidence="【get_positions】持股9只")
    user = msgs[1][1]
    assert "工具结果原文" in user and "【get_positions】" in user
    assert "need_tools" not in user          # 阶段二不再请求下钻


def test_generate_two_phase_drilldown():
    """第一轮返回 need_tools → 第二轮带原文；最终采用第二轮的仪表盘。"""
    from agents.analysis import dashboard_generator as dg

    seen = []

    async def fake_call(messages, budget, logger_obj, metadata=None, run_config=None):
        seen.append(messages[1][1])
        if len(seen) == 1:
            return {"need_tools": ["get_positions"], "reason": "缺持仓明细"}
        return {"scenario_tag": "决策仪表盘", "core_verdict": "x", "decision_type": "hold",
                "confidence_level": 0.5, "sentiment_score": 50, "trend_prediction": "neutral",
                "quality_tag": "完整分析", "checklist": [], "key_points": [], "next_watch": []}

    with patch.object(dg, "_call_llm", new=AsyncMock(side_effect=fake_call)):
        data = asyncio.run(dg.generate(
            "报告正文", {"get_positions": "持股9只"}, "t_x", "问题",
            tool_calls=[{"name": "get_positions", "tool_output": "持股9只"}],
        ))

    assert data is not None and len(seen) == 2
    assert "工具结果索引" in seen[0] and "need_tools" in seen[0]
    assert "工具结果原文" in seen[1] and "need_tools" not in seen[1]


def test_generate_single_phase_when_enough():
    """第一轮直接给出仪表盘 → 只调一次 LLM。"""
    from agents.analysis import dashboard_generator as dg

    calls = []

    async def fake_call(messages, budget, logger_obj, metadata=None, run_config=None):
        calls.append(1)
        return {"scenario_tag": "决策仪表盘", "core_verdict": "x", "decision_type": "hold",
                "confidence_level": 0.5, "sentiment_score": 50, "trend_prediction": "neutral",
                "quality_tag": "完整分析", "checklist": [], "key_points": [], "next_watch": []}

    with patch.object(dg, "_call_llm", new=AsyncMock(side_effect=fake_call)):
        data = asyncio.run(dg.generate("报告正文", {"get_positions": "持股9只"}, "t_x", "问题"))

    assert data is not None and len(calls) == 1


# ══════════════════════════════════════════════════════════════
# 失败工具不得当证据 / 方向不得由弱规则预置（2026-10-01 事故）
#   实测链：失败的大宗交易返回含「机构席位」→ 被 _find_slot_values 命中「资金面」
#   → detail 原样展示失败 JSON；叠加新闻槽后状态变 positive（状态与详情不同源）；
#   `_extract_decision_type` 关键词计数又预置 decision_type=buy →「综合分析建议买入」。
# ══════════════════════════════════════════════════════════════

_FAILED_TOOL_OUTPUT = (
    '{"query": "2026年9月 大宗交易 机构席位 成交 折价", '
    '"error": "接口返回中无 dataTableDTOList", "status": "failed"}'
)
_NEWS_TOOL_OUTPUT = "**来源：证券时报｜龙虎榜 游资席位 净买入** 共12条资讯；9/30沪深两市主力净流出…"


def test_find_slot_values_skips_failed_output():
    """失败输出的文案（机构席位）不得让它变成「资金面」命中"""
    from agents.analysis.dashboard_generator import _find_slot_values
    slots = {"mx_data_query": _FAILED_TOOL_OUTPUT}
    assert _find_slot_values(slots, ("资金", "主力", "机构", "换手")) == []


def test_checklist_skips_failed_output():
    """只看失败数据时不得产出 checklist 项"""
    from agents.analysis.dashboard_generator import _extract_checklist
    assert _extract_checklist({"mx_data_query": _FAILED_TOOL_OUTPUT}) == []


def test_checklist_detail_shares_source_with_status():
    """失败 JSON + 新闻：状态与详情必须同源（detail 不能是失败 JSON）"""
    from agents.analysis.dashboard_generator import _extract_checklist
    items = _extract_checklist({"mx_data_query": _FAILED_TOOL_OUTPUT,
                               "mx_search_news": _NEWS_TOOL_OUTPUT})
    fund = [it for it in items if it.dimension == "资金面"]
    assert fund, "新闻槽含资金关键词，应产出资金面项"
    assert "dataTableDTOList" not in fund[0].detail
    assert "接口返回中无" not in fund[0].detail
    assert "龙虎榜" in fund[0].detail or "主力" in fund[0].detail


def test_slot_results_prefers_usable_over_failed():
    """同名工具：可用输出优先，不被随后的失败覆盖；失败不覆盖已取到的数据"""
    from agents.analysis.dashboard_node import _tool_calls_to_slot_results
    usable = {"tool_name": "mx_data_query", "tool_output": '{"tables_count":1,"total_rows":21}'}
    failed = {"tool_name": "mx_data_query", "tool_output": _FAILED_TOOL_OUTPUT}

    # 先成功后失败 → 保留成功
    slots = _tool_calls_to_slot_results([usable, failed])
    assert "dataTableDTOList" not in slots["mx_data_query"]
    # 先失败后成功 → 被成功覆盖
    slots2 = _tool_calls_to_slot_results([failed, usable])
    assert "dataTableDTOList" not in slots2["mx_data_query"]


def test_verdict_map_wording_is_not_directive():
    """兜底路径的 core_verdict 不得出现"建议买入/卖出"这类指令性表述"""
    from agents.analysis.dashboard_generator import generate as _gen

    sell_heavy = {"风险分析": "止损 减仓 破位 下跌 走弱 卖出 减持 看空"}
    with patch.object(Config, "DASHBOARD_LLM_ENABLED", False):
        data = asyncio.run(_gen(
            report_content="报告", slot_results=sell_heavy,
            template_id="stock_deep_dive", user_input="分析贵州茅台",
        ))

    if data is not None and data.core_verdict:
        assert "建议买入" not in data.core_verdict
        assert "建议卖出" not in data.core_verdict


def test_llm_prompt_excludes_direction_hints():
    """方向类规则结论不得进入 LLM 提示词（只留给兜底路径）"""
    from agents.analysis.dashboard_generator import generate as _gen

    captured = {}

    async def _fake_call_llm(messages, budget, logger_obj, **kwargs):
        captured["messages"] = messages
        return None

    sell_heavy = {"风险分析": "止损 减仓 破位 下跌 走弱 卖出 减持 看空"}
    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch("agents.analysis.dashboard_generator._call_llm", side_effect=_fake_call_llm):
        asyncio.run(_gen(
            report_content="报告", slot_results=sell_heavy,
            template_id="stock_deep_dive", user_input="分析贵州茅台",
        ))

    blob = " ".join(str(m) for m in captured.get("messages", []))
    # 注意：`decision_type` 作为**输出 schema 字段名**本就在 system prompt 里，不能整体断言。
    # 要断言的是「规则预提取参考」那一段里没有预置方向。
    marker = "规则预提取参考"
    if marker in blob:
        hints_block = blob.split(marker, 1)[1]
        assert "- decision_type" not in hints_block, "方向类规则结论不得预置给 LLM"
        assert "- core_verdict" not in hints_block
        assert "建议买入" not in hints_block
        assert "规则参考" not in hints_block
    else:
        # 连提示块都没有：更彻底（未产出任何规则提示）
        assert "建议买入" not in blob


# ============================================================
# 直接运行
# ============================================================

# ══════════════════════════════════════════════════════════════
# 轻量仪表盘 quick（2026-10-01，配套 quick_lookup 模板）
# ══════════════════════════════════════════════════════════════

def test_quick_dashboard_def_registered_and_minimal():
    from agents.analysis.dashboard_config import (
        get_dashboard_category, get_dashboard_def, list_dashboards)
    from agents.analysis.template_store import load_template

    assert "quick" in list_dashboards()
    d = get_dashboard_def("quick")
    assert d is not None and d["scenario_tag"] == "速览仪表盘"
    # 轻量：不预置任何扩展字段（方向/仓位/止损类），只用基础字段渲染
    assert d.get("llm_fields") == []
    assert [s["renderer"] for s in d.get("sections", [])] == ["verdict_text", "key_points"]

    cat = get_dashboard_category("quick_lookup", load_template("quick_lookup"))
    assert cat.dashboard_id == "quick"
    assert cat.scenario_tag == "速览仪表盘"


if __name__ == "__main__":
    import traceback

    tests = [
        test_schema_valid_data,
        test_schema_invalid_decision_type,
        test_schema_invalid_confidence_level,
        test_schema_invalid_sentiment_score,
        test_schema_empty_checklist,
        test_schema_only_common_fields,
        test_schema_stock_extended_fields,
        test_generator_llm_success,
        test_generator_llm_invalid_json,
        test_generator_llm_partial_fields,
        test_generator_llm_timeout,
        test_generator_budget_exhausted,
        test_generator_rule_extract_success,
        test_generator_rule_extract_no_data,
        test_generator_short_report,
        test_generator_long_report,
        test_generator_llm_disabled,
        test_config_dashboard_enabled,
        test_config_dashboard_disabled,
        test_config_llm_disabled,
        test_unknown_template_defaults_to_stock,
        test_sector_scenes_have_correct_category,
        test_market_scenes_have_correct_category,
        test_stock_scenes_have_correct_category,
        test_all_enabled_templates_have_dashboard_category,
        test_dashboard_node_handles_dividend_screening_template,
        test_dashboard_node_uses_react_exec_state_tool_calls,
        test_generator_coerces_string_extended_fields,
        test_cli_format_complete,
        test_cli_no_web_marker,
        test_cli_no_price_levels,
        test_cli_at_end,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"[PASS] {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test.__name__}: {e}")
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("[OK] 所有仪表盘测试通过!")
    print(f"{'=' * 60}")
    sys.exit(1 if failed > 0 else 0)

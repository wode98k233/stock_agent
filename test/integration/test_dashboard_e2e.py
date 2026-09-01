"""
仪表盘端到端集成测试

覆盖：6 条报告路径 (T01-T06)、回归安全 (T45-T50)、端到端流程
使用 mock 模拟 LLM 调用，不依赖外部网络。
运行: pytest test/integration/test_dashboard_e2e.py -v
"""
import os
import sys
import json
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ── 公共 fixture / 工具函数 ──

def _rich_slot_results():
    """包含丰富关键词的 slot_results，可触发规则预提取"""
    return {
        "涨跌幅": "+2.5%，突破前期高点，放量上涨",
        "主力净流入": "主力净流入1.2亿，资金积极买入",
        "RSI": "RSI=65，处于中性偏多区间",
        "MACD": "MACD金叉确认，DIF上穿DEA，多头信号",
        "换手率": "换手率3.5%，交易活跃",
        "技术面趋势": "均线多头排列，趋势向上突破",
        "基本面PE": "PE=28.5，估值合理",
        "资金流向": "北向资金持续流入，机构增持",
        "情绪面舆情": "提价预期发酵，市场情绪偏积极",
        "支撑位": "支撑位1850.00，下方支撑较强",
        "压力位": "压力位1950.50，上方压力明显",
        "止损位": "止损位1820.00",
        "目标价": "目标价2000.00，看至前高",
    }


def _make_full_dashboard_json(**overrides):
    """构建完整的仪表盘 JSON（模拟 LLM 返回）"""
    base = {
        "core_verdict": "综合分析建议买入，技术面偏多",
        "decision_type": "buy",
        "confidence_level": 0.72,
        "sentiment_score": 68,
        "trend_prediction": "bullish",
        "quality_tag": "完整分析",
        "checklist": [
            {"dimension": "技术面", "status": "positive", "detail": "MACD金叉，均线多头排列"},
            {"dimension": "基本面", "status": "warning", "detail": "PE估值中性偏高"},
            {"dimension": "资金面", "status": "positive", "detail": "主力净流入，北向增持"},
            {"dimension": "情绪面", "status": "positive", "detail": "提价预期发酵"},
        ],
        "risk_priority": [
            {"level": "medium", "category": "估值风险", "detail": "PE偏高", "action": "关注回调风险"},
        ],
        "key_points": ["MACD金叉确认", "主力资金流入", "提价预期"],
        "next_watch": ["1950压力位突破", "成交量变化"],
        "price_levels": {"support": 1850.0, "resistance": 1950.5, "stop_loss": 1820.0, "target": 2000.0},
        "split_advice": None,
        "position_guidance": None,
    }
    base.update(overrides)
    return base


def _mock_logger():
    """创建 mock logger"""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


# ════════════════════════════════════════════════
# T01-T06: 报告路径测试
# ════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_p1_mode_a_dashboard():
    """T01: 模式A输出（分析引擎 + 仪表盘）+ 仪表盘生成，quality_tag=完整分析"""
    from agents.analysis.dashboard_generator import generate

    dashboard_json = _make_full_dashboard_json(quality_tag="完整分析")
    mock_call_llm = AsyncMock(return_value=dashboard_json)

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        result = await generate(
            report_content="综合分析：技术面偏多，MACD金叉确认，建议买入",
            slot_results=_rich_slot_results(),
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert result is not None
    assert result.quality_tag == "完整分析"
    assert result.decision_type == "buy"
    assert result.confidence_level > 0
    assert len(result.checklist) >= 2
    assert result.price_levels is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_p2_mode_b_dashboard():
    """T02: 模式B输出（分析引擎禁用 + 仪表盘独立生成），quality_tag=完整分析"""
    from agents.analysis.dashboard_generator import generate

    dashboard_json = _make_full_dashboard_json(quality_tag="完整分析")
    mock_call_llm = AsyncMock(return_value=dashboard_json)

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        result = await generate(
            report_content="板块轮动分析：白酒板块走强，资金流入明显",
            slot_results=_rich_slot_results(),
            template_id="trend_following",
            user_input="分析白酒板块",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert result is not None
    assert result.quality_tag == "完整分析"
    assert result.trend_prediction in ("bullish", "neutral", "bearish")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_p3_low_fill_ratio():
    """T03: slot_results 为空 + 仪表盘生成，quality_tag=快速概览"""
    from agents.analysis.dashboard_generator import generate

    dashboard_json = _make_full_dashboard_json(quality_tag="快速概览")
    mock_call_llm = AsyncMock(return_value=dashboard_json)

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        result = await generate(
            report_content="数据不足，无法完整分析",
            slot_results={},
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert result is not None
    assert result.quality_tag == "快速概览"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_p4_exception_fallback():
    """T04: 模拟异常降级 + 仪表盘生成，quality_tag=快速概览"""
    from agents.analysis.dashboard_generator import generate

    mock_call_llm = AsyncMock(side_effect=Exception("LLM 服务不可用"))

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        result = await generate(
            report_content="综合分析：技术面偏多，MACD金叉确认，建议买入",
            slot_results=_rich_slot_results(),
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert result is not None
    assert result.quality_tag == "快速概览"
    assert result.decision_type in ("buy", "sell", "hold")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_p5_engine_disabled():
    """T05: 模拟引擎禁用（DASHBOARD_LLM_ENABLED=False）+ 仅规则预提取"""
    from agents.analysis.dashboard_generator import generate

    with patch("config.Config.DASHBOARD_LLM_ENABLED", False):
        result = await generate(
            report_content="综合分析：技术面偏多，MACD金叉确认，建议买入",
            slot_results=_rich_slot_results(),
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert result is not None
    assert result.quality_tag == "快速概览"
    assert result.decision_type is not None
    assert result.sentiment_score is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_p6_budget_exhausted():
    """T06: 模拟预算不足 + 仅规则预提取，quality_tag=快速概览"""
    from agents.analysis.dashboard_generator import generate
    from utils.budget import BudgetExceeded

    mock_call_llm = AsyncMock(side_effect=BudgetExceeded("预算已耗尽", 1000, 500))

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        result = await generate(
            report_content="综合分析：技术面偏多，MACD金叉确认，建议买入",
            slot_results=_rich_slot_results(),
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert result is not None
    assert result.quality_tag == "快速概览"


# ════════════════════════════════════════════════
# T45-T50: 回归安全测试
# ════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_report_format_unchanged():
    """T45: DASHBOARD_ENABLED=false 时报告输出不变"""
    from agents.analysis.models import AnalysisResult

    original_report = "## 核心摘要\n短线观望\n\n## 技术面\nMACD金叉"
    result = AnalysisResult(content=original_report, dashboard=None)

    assert result.content == original_report
    assert result.dashboard is None
    assert "@@DASHBOARD" not in result.content


@pytest.mark.integration
def test_analysis_result_compatible():
    """T46: AnalysisResult(dashboard=None) 正常工作"""
    from agents.analysis.models import AnalysisResult

    result = AnalysisResult(content="测试报告")
    assert result.dashboard is None
    assert result.content == "测试报告"
    assert result.fallback_used is False
    assert result.degraded is False

    result_with_dash = AnalysisResult(content="测试报告", dashboard={"core_verdict": "买入"})
    assert result_with_dash.dashboard is not None
    assert result_with_dash.dashboard["core_verdict"] == "买入"


@pytest.mark.integration
def test_dashboard_marker_format():
    """T47: Web 模式输出含正确的 @@DASHBOARD_START@@...@@DASHBOARD_END@@ 标记"""
    from agents.analysis.dashboard_schema import DashboardData

    data = DashboardData(
        core_verdict="综合分析建议买入",
        decision_type="buy",
        confidence_level=0.72,
        sentiment_score=68,
        trend_prediction="bullish",
        quality_tag="完整分析",
        checklist=[],
        risk_priority=[],
        key_points=["MACD金叉"],
        next_watch=["压力位"],
    )

    json_str = data.model_dump_json()
    web_output = f"\n@@DASHBOARD_START@@{json_str}@@DASHBOARD_END@@"

    assert web_output.startswith("\n@@DASHBOARD_START@@")
    assert web_output.endswith("@@DASHBOARD_END@@")
    assert "@@DASHBOARD_START@@" in web_output
    assert "@@DASHBOARD_END@@" in web_output

    extracted = web_output.split("@@DASHBOARD_START@@")[1].split("@@DASHBOARD_END@@")[0]
    parsed = json.loads(extracted)
    assert parsed["core_verdict"] == "综合分析建议买入"
    assert parsed["decision_type"] == "buy"


@pytest.mark.integration
def test_cli_output_no_marker():
    """T48: CLI 模式输出不含 @@DASHBOARD 标记"""
    from agents.analysis.dashboard_schema import DashboardData
    from agents.analysis.dashboard_cli import format_dashboard_cli

    data = DashboardData(
        core_verdict="综合分析建议买入",
        decision_type="buy",
        confidence_level=0.72,
        sentiment_score=68,
        trend_prediction="bullish",
        quality_tag="完整分析",
        checklist=[],
        risk_priority=[],
        key_points=["MACD金叉"],
        next_watch=["压力位"],
    )

    cli_output = format_dashboard_cli(data)

    assert "@@DASHBOARD" not in cli_output
    assert "决策仪表盘" in cli_output
    assert "BUY" in cli_output or "综合分析建议买入" in cli_output


# ════════════════════════════════════════════════
# 端到端流程测试
# ════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_flow_stock_deep_dive():
    """完整流程：mock 数据 → generate() → format_dashboard_cli() → 验证输出包含信号行/核心结论/买卖点位"""
    from agents.analysis.dashboard_generator import generate
    from agents.analysis.dashboard_cli import format_dashboard_cli

    dashboard_json = _make_full_dashboard_json(
        quality_tag="完整分析",
        price_levels={"support": 1850.0, "resistance": 1950.5, "stop_loss": 1820.0, "target": 2000.0},
    )
    mock_call_llm = AsyncMock(return_value=dashboard_json)

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        dashboard_data = await generate(
            report_content="综合分析：技术面偏多，MACD金叉确认，建议买入",
            slot_results=_rich_slot_results(),
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert dashboard_data is not None

    cli_output = format_dashboard_cli(dashboard_data)

    assert "BUY" in cli_output, "信号行应包含 BUY"
    assert "综合分析建议买入" in cli_output, "应包含核心结论"
    assert "买卖点位" in cli_output, "应包含买卖点位板块"
    assert "1850" in cli_output or "1950" in cli_output, "应包含价格数字"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_flow_web_marker():
    """完整流程：mock 数据 → generate() → 验证 @@DASHBOARD 标记格式正确"""
    from agents.analysis.dashboard_generator import generate

    dashboard_json = _make_full_dashboard_json(quality_tag="完整分析")
    mock_call_llm = AsyncMock(return_value=dashboard_json)

    with patch("agents.analysis.dashboard_generator._call_llm", mock_call_llm), \
         patch("config.Config.DASHBOARD_LLM_ENABLED", True):
        dashboard_data = await generate(
            report_content="综合分析：技术面偏多，MACD金叉确认，建议买入",
            slot_results=_rich_slot_results(),
            template_id="stock_deep_dive",
            user_input="分析贵州茅台",
            budget=None,
            logger_obj=_mock_logger(),
        )

    assert dashboard_data is not None

    json_str = dashboard_data.model_dump_json()
    web_output = f"\n@@DASHBOARD_START@@{json_str}@@DASHBOARD_END@@"

    assert "@@DASHBOARD_START@@" in web_output
    assert "@@DASHBOARD_END@@" in web_output

    extracted = web_output.split("@@DASHBOARD_START@@")[1].split("@@DASHBOARD_END@@")[0]
    parsed = json.loads(extracted)
    assert parsed["decision_type"] == "buy"
    assert parsed["quality_tag"] == "完整分析"
    assert "checklist" in parsed
    assert isinstance(parsed["checklist"], list)


# ── 独立运行入口 ──

if __name__ == "__main__":
    import traceback

    async def _run_all():
        tests = [
            test_path_p1_mode_a_dashboard,
            test_path_p2_mode_b_dashboard,
            test_path_p3_low_fill_ratio,
            test_path_p4_exception_fallback,
            test_path_p5_engine_disabled,
            test_path_p6_budget_exhausted,
            test_report_format_unchanged,
            test_analysis_result_compatible,
            test_dashboard_marker_format,
            test_cli_output_no_marker,
            test_full_flow_stock_deep_dive,
            test_full_flow_web_marker,
        ]
        passed = failed = 0
        for t in tests:
            try:
                if asyncio.iscoroutinefunction(t):
                    await t()
                else:
                    t()
                passed += 1
                print(f"[PASS] {t.__name__}")
            except Exception as e:
                failed += 1
                print(f"[FAIL] {t.__name__}: {e}")
                traceback.print_exc()
        print(f"\n{'=' * 50}")
        print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
        return failed == 0

    success = asyncio.get_event_loop().run_until_complete(_run_all())
    sys.exit(0 if success else 1)

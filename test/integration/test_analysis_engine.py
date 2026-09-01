"""
分析框架引擎集成测试

验证完整后处理流程，使用伪造 ToolCall，不依赖外部网络。
运行: pytest test/integration/test_analysis_engine.py -v
"""
import os
import sys
import json
import asyncio
import pytest
from unittest.mock import MagicMock, patch

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _mock_llm_module(content="## 核心摘要\n短线观望\n\n## 技术面\nMACD金叉\n\n## 综合研判\n建议观望\n\n## 风险提示\n市场有风险"):
    """创建 mock 的 utils.llm_factory 模块"""
    mock_mod = MagicMock()
    mock_mod.get_llm = MagicMock(return_value=MagicMock())
    mock_resp = MagicMock()
    mock_resp.content = content
    mock_mod.tracked_invoke = MagicMock(return_value=mock_resp)
    mock_mod.llm_json_with_retry = MagicMock(return_value=None)
    return mock_mod


def _make_tc(tool_name, tool_input, tool_output):
    tc = MagicMock()
    tc.tool_name = tool_name
    tc.tool_input = tool_input
    tc.tool_output = tool_output
    return tc


def _make_request():
    from agents.analysis.models import AnalysisRequest
    return AnalysisRequest(
        user_input="分析贵州茅台",
        agent_name="react_stock",
        logger=MagicMock(),
        budget=None,
    )


@pytest.mark.asyncio
async def test_run_analysis_full_pipeline():
    """完整管线：模板→证据→提取→评估→规则化报告"""
    from agents.analysis.engine import run_analysis

    tool_calls = [
        _make_tc("technical_analysis", "贵州茅台", json.dumps({
            "rsi": 65.3, "DIF": 0.05, "DEA": 0.03, "MACD": 0.02,
            "trend": "上涨", "change_pct": 1.5,
        })),
        _make_tc("valuation", "贵州茅台", json.dumps({
            "pe": 32.5, "pe_percentile": 72,
        })),
        _make_tc("mx_search", "茅台新闻", "提价预期发酵，市场关注度提升。"),
    ]

    mock_mod = _mock_llm_module()
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        result = await run_analysis(_make_request(), tool_calls)

    assert result.content
    assert len(result.content) > 50
    assert result.used_template == "standard"
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_run_analysis_no_tools_fallback():
    """无工具调用 → 降级"""
    from agents.analysis.engine import run_analysis

    mock_mod = _mock_llm_module()
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        result = await run_analysis(_make_request(), [], raw_result="原始输出")

    assert result.content
    assert result.used_template == "standard"


@pytest.mark.asyncio
async def test_run_analysis_exception_fallback():
    """内部异常 → 降级返回原始结果"""
    from agents.analysis.engine import run_analysis

    with patch("agents.analysis.template_store.load_template", side_effect=Exception("模板损坏")):
        result = await run_analysis(_make_request(), [], raw_result="原始结果")

    assert result.fallback_used is True
    assert result.content == "原始结果"
    assert result.degraded is True


@pytest.mark.asyncio
async def test_run_analysis_diagnostics():
    """diagnostics 包含各阶段信息"""
    from agents.analysis.engine import run_analysis

    tool_calls = [
        _make_tc("technical_analysis", "test", json.dumps({"rsi": 50})),
    ]

    mock_mod = _mock_llm_module()
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        result = await run_analysis(_make_request(), tool_calls)

    assert "template" in result.diagnostics
    assert "evidence" in result.diagnostics
    assert "extract" in result.diagnostics
    assert "evaluate" in result.diagnostics


@pytest.mark.asyncio
async def test_run_analysis_report_content():
    """报告内容包含必要板块"""
    from agents.analysis.engine import run_analysis

    tool_calls = [
        _make_tc("technical_analysis", "test", json.dumps({
            "rsi": 65, "DIF": 0.05, "DEA": 0.03, "MACD": 0.02,
        })),
    ]

    mock_mod = _mock_llm_module()
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        result = await run_analysis(_make_request(), tool_calls)

    content = result.content
    assert "摘要" in content or "核心" in content


@pytest.mark.asyncio
async def test_run_analysis_low_slot_fill_skips():
    """插槽提取率过低时跳过分析引擎，返回原始结果"""
    from agents.analysis.engine import run_analysis

    # 只提供一个工具，且输出不含可提取数据
    tool_calls = [
        _make_tc("mx_search", "test", "这是一条普通新闻，没有结构化数据"),
    ]

    mock_mod = _mock_llm_module()
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        result = await run_analysis(_make_request(), tool_calls, raw_result="原始分析结果内容")

    # 提取率低于 20%，应跳过分析引擎
    assert result.fallback_used is True
    assert result.content == "原始分析结果内容"
    assert result.diagnostics.get("skip_reason") == "low_slot_fill_ratio"


@pytest.mark.asyncio
async def test_run_analysis_raw_result_in_prompt():
    """raw_result 传入后 LLM prompt 应包含原始分析"""
    from agents.analysis.engine import run_analysis
    from agents.analysis.report_builder import _build_prompt

    tool_calls = [
        _make_tc("technical_analysis", "test", json.dumps({
            "rsi": 65, "DIF": 0.05, "DEA": 0.03, "MACD": 0.02,
            "trend": "上涨", "change_pct": 2.5,
        })),
    ]

    # 验证 raw_result 被传入 prompt
    from agents.analysis.template_store import load_template, evaluate
    from agents.analysis.models import SlotResult

    template = load_template("standard")
    slots = {
        "trend": SlotResult(slot="trend", value=2.5, status="上涨", interpretation="涨2.5%",
                           source_tool="test", method="json", confidence=0.8),
    }
    adjusted = evaluate(template, slots)
    long_raw = "原始分析：" + "茅台今日技术面偏多，MACD金叉确认，RSI处于中性区间。成交量放大，资金积极介入。" * 10
    messages = _build_prompt(adjusted, slots, "分析茅台", raw_result=long_raw)
    user_msg = messages[1][1]
    assert "原始分析报告" in user_msg
    assert "茅台今日技术面偏多" in user_msg


if __name__ == "__main__":
    import traceback
    tests = [
        test_run_analysis_full_pipeline,
        test_run_analysis_no_tools_fallback,
        test_run_analysis_exception_fallback,
        test_run_analysis_diagnostics,
        test_run_analysis_report_content,
    ]
    passed = failed = 0
    for t in tests:
        try:
            asyncio.get_event_loop().run_until_complete(t())
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed > 0 else 0)

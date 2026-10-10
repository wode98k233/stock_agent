"""raw_result_is_seed 的跨层透传（2026-10-01）

语义：agent_group 的原始数据走「条目 → 下钻」通道，raw_result 只作下钻种子；
其余四个模式（React / Unified / Plan / PDOR）必须照旧把 raw_result 注入报告 prompt
（它是报告草稿：`state["final_result"]` / `response`）。

这个语义靠一个布尔参数从 group 节点传到 `report_builder._build_prompt`，中间经过
`common.run_report_post_processing` → `engine.run_analysis` → `build` 三层。
任何一层把 kwarg 丢掉，都会**静默**改变报告层内容（表现为 raw_result 被重复注入或丢失），
所以逐层钉住。

运行: pytest test/unit/analysis/test_report_raw_result_plumbing.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _analysis_request():
    from agents.analysis.models import AnalysisRequest
    return AnalysisRequest(
        user_input="给我市场的9月总结",
        agent_name="agent_group",
        logger=MagicMock(),
        budget=None,
        template_id="theme_trading",
        selected_skills=[],
    )


def test_run_report_post_processing_forwards_flag():
    """common 层：raw_result_is_seed 必须原样转给 run_analysis"""
    captured = {}

    async def fake_run_analysis(request, tool_calls, raw_result, **kwargs):
        captured.update(kwargs)
        captured["raw_result"] = raw_result
        return SimpleNamespace(content="报告", fallback_used=False)

    with patch("agents.analysis.run_analysis", side_effect=fake_run_analysis), \
         patch("agents.common.Config") as mock_cfg:
        mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
        from agents.common import run_report_post_processing
        out = asyncio.run(run_report_post_processing(
            user_input="给我市场的9月总结",
            agent_name="agent_group",
            tool_calls=[{"tool_name": "数据底版 · 数据采集员", "tool_output": "x"}],
            raw_result="数据底版全文",
            template_id="theme_trading",
            selected_skills=[],
            logger=MagicMock(),
            raw_result_is_seed=True,
        ))

    assert out == "报告"
    assert captured["raw_result_is_seed"] is True
    assert captured["raw_result"] == "数据底版全文"


def test_run_report_post_processing_default_is_false():
    """默认必须是 False（其余四个模式的行为不能被改变）"""
    captured = {}

    async def fake_run_analysis(request, tool_calls, raw_result, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="报告", fallback_used=False)

    with patch("agents.analysis.run_analysis", side_effect=fake_run_analysis), \
         patch("agents.common.Config") as mock_cfg:
        mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
        from agents.common import run_report_post_processing
        asyncio.run(run_report_post_processing(
            user_input="q", agent_name="react_stock", tool_calls=[],
            raw_result="草稿", template_id="standard", selected_skills=[],
            logger=MagicMock(),
        ))

    assert captured["raw_result_is_seed"] is False


def test_engine_forwards_flag_to_build():
    """engine 层：raw_result_is_seed 必须原样转给 report_builder.build"""
    captured = {}

    async def fake_build(template, slot_results, user_input, **kwargs):
        captured.update(kwargs)
        return {"title": "T", "summary": "", "sections": [], "disclaimer": ""}

    with patch("agents.analysis.report_builder.build", side_effect=fake_build):
        from agents.analysis.engine import run_analysis
        asyncio.run(run_analysis(
            request=_analysis_request(),
            tool_calls=[{"tool_name": "mx_data_query", "tool_output": '{"tables_count":1}'}],
            raw_result="数据底版全文",
            raw_result_is_seed=True,
        ))

    assert captured.get("raw_result_is_seed") is True


def test_prompt_default_keeps_legacy_injection_rule():
    """report_builder 层：默认参数下，未传 seed 标记时 raw_result 照旧注入"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template("theme_trading"), {})
    raw = "报告草稿：市场缩量整理。" * 60
    messages = _build_prompt(template, {}, "今天大盘怎么样",
                             raw_result=raw,
                             tool_evidence="### mx_data_query\n输出:\n有数据",
                             step_results=[])
    assert "## 原始素材" in " ".join(m[1] for m in messages)

"""
数据模型单元测试

运行: pytest test/unit/analysis/test_models.py -v
"""
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def test_analysis_request_defaults():
    """AnalysisRequest 默认值正确"""
    from agents.analysis.models import AnalysisRequest
    from unittest.mock import MagicMock

    req = AnalysisRequest(user_input="分析茅台", agent_name="react_stock", logger=MagicMock())
    assert req.user_input == "分析茅台"
    assert req.agent_name == "react_stock"
    assert req.memory is None
    assert req.budget is None
    assert req.template_id is None
    assert req.horizon is None
    assert req.report_mode == "fast"


def test_slot_result_fields():
    """SlotResult 字段完整"""
    from agents.analysis.models import SlotResult

    sr = SlotResult(
        slot="rsi_status", value=65.3, status="偏强",
        interpretation="RSI 65.3", source_tool="tech",
        method="json", confidence=0.9,
    )
    assert sr.slot == "rsi_status"
    assert sr.value == 65.3
    assert sr.method == "json"
    assert sr.confidence == 0.9
    assert sr.evidence_refs == []
    assert sr.metadata == {}


def test_section_plan_fields():
    """SectionPlan 字段完整"""
    from agents.analysis.models import SectionPlan

    sp = SectionPlan(
        id="technical", title="技术面", prompt="分析趋势",
        status="ok", slot_results={}, max_words=200,
    )
    assert sp.id == "technical"
    assert sp.status == "ok"
    assert sp.max_words == 200


def test_report_section_fields():
    """ReportSection 字段完整"""
    from agents.analysis.models import ReportSection

    rs = ReportSection(id="summary", title="摘要", content="看多", status="ok")
    assert rs.id == "summary"
    assert rs.evidence_refs == []


def test_analysis_result_fields():
    """AnalysisResult 字段完整"""
    from agents.analysis.models import AnalysisResult

    ar = AnalysisResult(content="报告内容", used_template="standard")
    assert ar.content == "报告内容"
    assert ar.report is None
    assert ar.degraded is False
    assert ar.fallback_used is False
    assert ar.diagnostics == {}


def test_analysis_result_fallback():
    """AnalysisResult 降级场景"""
    from agents.analysis.models import AnalysisResult

    ar = AnalysisResult(
        content="原始输出",
        fallback_used=True,
        degraded=True,
        diagnostics={"error": "模板加载失败"},
    )
    assert ar.fallback_used is True
    assert ar.degraded is True
    assert "error" in ar.diagnostics


if __name__ == "__main__":
    import traceback
    tests = [
        test_analysis_request_defaults,
        test_slot_result_fields,
        test_section_plan_fields,
        test_report_section_fields,
        test_analysis_result_fields,
        test_analysis_result_fallback,
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

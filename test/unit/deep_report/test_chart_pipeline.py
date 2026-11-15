"""图表链路集成测试（离线）：卡片 → 候选 → 选择 → 编译 → 按节渲染。"""
from __future__ import annotations

import threading

from agents.deep_report.models import ReportMeta
from agents.deep_report.runtime import DeepReportRuntime
from agents.deep_report.store import DeepReportStore
from agents.subagent.models import STATUS_OK, SubagentResult


class StubExecutor:
    """按槽返回一份含横截面数据的结论卡。"""

    def __init__(self, cards):
        self.cards = cards

    def run(self, task, cancel=None):
        card = self.cards.get(task.slot)
        if card is None:
            return SubagentResult("no_data", "no-data")
        return SubagentResult(STATUS_OK, "completed", structured=card, tool_calls=1, turns=1)


def _meta(rid):
    return ReportMeta(rid=rid, universe="银行板块", user_request="", template_id="deep_sector_finance",
                      variant="finance", status="draft")


def test_charts_end_to_end(tmp_path):
    cards = {"snapshot": {"asof": "2026-10-30", "status": "ok", "items": [
        {"metric": "工行", "value": 0.71, "unit": "倍", "source": "mx", "period": "2026-10-30"},
        {"metric": "建行", "value": 0.68, "unit": "倍", "source": "mx", "period": "2026-10-30"},
        {"metric": "招行", "value": 1.10, "unit": "倍", "source": "mx", "period": "2026-10-30"},
        {"metric": "兴业", "value": 0.55, "unit": "倍", "source": "mx", "period": "2026-10-30"},
    ], "charts": [], "gaps": []}}

    store = DeepReportStore(tmp_path)
    rt = DeepReportRuntime(store=store, llm=None, executor=StubExecutor(cards),
                           fetch=True, qa_enabled=False, semantic_review=False)
    meta = rt.run(universe="银行板块", template_id="deep_sector_finance",
                  max_sections=1, cancel=threading.Event())
    rid = meta["rid"]

    # 1) 报告记了图
    assert meta["charts"] >= 1, f"应有图，实际 {meta['charts']}"
    # 2) 阅读页真的插了图位
    html = (store.dir_of(rid) / "report.html").read_text(encoding="utf-8")
    assert "data-chart-id" in html
    assert '"series"' in html
    # 3) 图跟着章节走（不是堆在文末）
    sec1 = html.split("id='sec1'")[1] if "id='sec1'" in html else html
    assert "data-chart-id='s1-" in sec1


class TestShapeValidation:
    def test_histogram_axis_mismatch_rejected(self):
        import pytest
        from agents.deep_report.chart_compiler import ChartSpecError, compile_spec
        spec = {"id": "h", "type": "histogram", "title": "t",
                "x": {"values": ["a", "b", "c"]},
                "series": [{"name": "n", "data": [1, 2]}],
                "source": "s", "asof": "2026-10-30"}
        with pytest.raises(ChartSpecError):
            compile_spec(spec)

    def test_pie_needs_single_number(self):
        import pytest
        from agents.deep_report.chart_compiler import ChartSpecError, compile_spec
        spec = {"id": "p", "type": "pie", "title": "t",
                "series": [{"name": "买入", "data": [3, 4]}],
                "source": "s", "asof": "2026-10-30"}
        with pytest.raises(ChartSpecError):
            compile_spec(spec)

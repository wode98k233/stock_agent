"""长报告 M1 骨架单测（纯逻辑，不触网、不调 LLM）。"""
from __future__ import annotations

import json
import threading

import pytest

from agents.deep_report import chart_compiler as cc
from agents.deep_report.models import ReportMeta, SectionResult
from agents.deep_report.outliner import build_outline
from agents.deep_report.render import build_report_html, md_to_html
from agents.deep_report.runtime import DeepReportRuntime
from agents.deep_report.store import DeepReportStore
from agents.deep_report.templates import TemplateError, load_template, section_specs


# ── 模板 ──
class TestTemplate:
    def test_load_and_sections(self):
        tpl = load_template("deep_sector_finance")
        assert tpl["template_id"].startswith("deep_")
        specs = section_specs(tpl)
        assert len(specs) >= 8
        assert specs[0]["section_id"] == "summary"
        assert all(s["seq"] == i for i, s in enumerate(specs, start=1))

    def test_missing_template_raises(self):
        with pytest.raises(TemplateError):
            load_template("deep_not_exists")


# ── 大纲（无 LLM 回落模板） ──
class TestOutline:
    def test_fallback_without_llm(self):
        tpl = load_template("deep_sector_finance")
        outline = build_outline(tpl, universe="银行板块", llm=None)
        assert outline.source == "template"
        assert len(outline.sections) == len(section_specs(tpl))
        assert outline.sections[0].status == "pending"


# ── 存储 ──
class TestStore:
    def test_roundtrip(self, tmp_path):
        store = DeepReportStore(tmp_path)
        rid = store.new_rid()
        meta = ReportMeta(rid=rid, universe="银行", user_request="", template_id="deep_sector_finance",
                          variant="finance", status="draft")
        store.create(meta)
        store.save_section(rid, SectionResult(1, "summary", "投资摘要", "ok", "## 1. 摘要\n正文", 20))
        store.finish(rid, "done", words=20, sections_done=1)
        again = store.read_meta(rid)
        assert again.status == "done" and again.words == 20
        assert (store.dir_of(rid) / "sections" / "01_summary.md").read_text(encoding="utf-8").startswith("## 1.")
        assert store.list_reports()[0]["rid"] == rid

    def test_list_skips_broken_json(self, tmp_path):
        store = DeepReportStore(tmp_path)
        bad = store.base / "bad"
        bad.mkdir(parents=True)
        (bad / "report.json").write_text("{not json", encoding="utf-8")
        assert store.list_reports() == []


# ── 图编译 ──
class TestChartCompiler:
    def test_rejects_missing_source(self):
        with pytest.raises(cc.ChartSpecError):
            cc.compile_spec({"type": "line", "series": [{"name": "a", "data": [1]}], "asof": "2026-10-30"})

    def test_rejects_missing_asof(self):
        with pytest.raises(cc.ChartSpecError):
            cc.compile_spec({"type": "line", "series": [{"name": "a", "data": [1]}], "source": "x"})

    def test_line_ok(self):
        opt = cc.compile_spec({"type": "line", "source": "iwc", "asof": "2026-10-30",
                               "x": {"values": ["1Q", "2Q"]},
                               "series": [{"name": "净息差", "data": [1.4, 1.5]}]})
        assert opt["series"][0]["type"] == "line"
        assert opt["xAxis"]["data"] == ["1Q", "2Q"]

    def test_combo_dual_axis(self):
        opt = cc.compile_spec({"type": "combo", "source": "s", "asof": "d",
                               "x": {"values": ["a"]},
                               "series": [{"name": "x", "kind": "bar", "axis": 1, "data": [1]},
                                          {"name": "y", "kind": "line", "axis": 2, "data": [2]}]})
        assert len(opt["yAxis"]) == 2
        assert opt["series"][1]["yAxisIndex"] == 1

    def test_compile_all_collects_errors(self):
        out = cc.compile_all([
            {"id": "ok", "type": "pie", "source": "s", "asof": "d", "series": [{"name": "a", "data": [3]}]},
            {"id": "bad", "type": "line", "series": [{"name": "a", "data": [1]}]},
        ])
        assert "ok" in out and "_errors" in out and "bad" in out["_errors"]


# ── 渲染 ──
class TestRender:
    def test_md_subset(self):
        html = md_to_html("## 标题\n\n- a\n- **b**\n\n| x | y |\n|---|---|\n| 1 | 2 |")
        assert "<h2>标题</h2>" in html and "<strong>b</strong>" in html
        assert "<table>" in html and "<td>1</td>" in html

    def test_report_html_has_toc_and_sections(self, tmp_path):
        tpl = load_template("deep_sector_finance")
        outline = build_outline(tpl, universe="银行板块", llm=None)
        secs = [SectionResult(1, "summary", "投资摘要", "ok", "## 1. 摘要\n正文", 12)]
        meta = ReportMeta(rid="r1", universe="银行板块", user_request="", template_id="deep_sector_finance",
                          variant="finance", status="done", words=12)
        doc = build_report_html(meta, outline, secs)
        assert "#sec1" in doc and "投资摘要" in doc and "echarts" in doc

    def test_charts_placed_in_section(self):
        tpl = load_template("deep_sector_finance")
        outline = build_outline(tpl, universe="银行板块", llm=None)
        secs = [SectionResult(1, "summary", "投资摘要", "ok", "## 1. 摘要\n正文", 12)]
        meta = ReportMeta(rid="r2", universe="银行板块", user_request="",
                          template_id="deep_sector_finance", variant="finance", status="done", words=12)
        doc = build_report_html(meta, outline, secs,
                                charts={1: {"c1": {"series": [{"type": "bar", "data": [1]}],
                                                  "xAxis": {"data": ["a"]}}}})
        assert "data-chart-id='s1-c1'" in doc
        assert '"s1-c1"' in doc


# ── 编排（stub，不调 LLM） ──
class TestRuntimeStub:
    def test_run_three_sections(self, tmp_path):
        store = DeepReportStore(tmp_path)
        seen = []
        rt = DeepReportRuntime(store=store, llm=None, progress=lambda e, p: seen.append(e))
        meta = rt.run(universe="银行板块", template_id="deep_sector_finance",
                      max_sections=3, cancel=threading.Event())
        assert meta["status"] == "done"
        assert meta["sections_done"] == 3
        d = store.dir_of(meta["rid"])
        assert (d / "report.md").exists() and (d / "report.html").exists()
        assert (d / "outline.json").exists()
        assert "section" in seen and "done" in seen

    def test_done_event_emitted_before_finish(self, tmp_path, monkeypatch):
        """评审 P1-1 回归：done 事件必须先于 finish 落库。

        反序时 /events 会在「无事件 + 状态终态」那拍收流，done 慢于宽限即永久丢失。
        """
        from agents.deep_report.store import DeepReportStore as _S
        order = []
        orig_emit = DeepReportRuntime._emit
        orig_finish = _S.finish

        def emit(self, rid, etype, data=None):
            if etype == "done":
                order.append("emit:done")
            return orig_emit(self, rid, etype, data)

        def finish(self, rid, status, **kw):
            order.append("finish")
            return orig_finish(self, rid, status, **kw)

        monkeypatch.setattr(DeepReportRuntime, "_emit", emit)
        monkeypatch.setattr(_S, "finish", finish)
        store = _S(tmp_path)
        rt = DeepReportRuntime(store=store, llm=None)
        rt.run(universe="银行板块", template_id="deep_sector_finance",
               max_sections=1, cancel=threading.Event())
        assert "emit:done" in order and "finish" in order
        assert order.index("emit:done") < order.index("finish")

    def test_cancel_between_sections(self, tmp_path):
        store = DeepReportStore(tmp_path)
        cancel = threading.Event()

        def on_progress(ev, payload):
            if ev == "section" and payload.get("index") == 1:
                cancel.set()

        rt = DeepReportRuntime(store=store, llm=None, progress=on_progress)
        meta = rt.run(universe="银行板块", max_sections=5, cancel=cancel)
        assert meta["status"] == "cancelled"
        assert meta["sections_done"] == 1

"""M2 单测：子代理执行器 + 取数降级链 + 注入位置（全部离线）。"""
from __future__ import annotations

import threading
import time

import pytest

from agents.deep_report.fetch import (
    CARD_SCHEMA, FetchService, summary_lines, validate_card,
)
from agents.deep_report.models import Outline, OutlineSection
from agents.deep_report.store import DeepReportStore
from agents.deep_report.writer import _user_prompt
from agents.subagent.executor import SubagentExecutor
from agents.subagent.models import (
    STATUS_ABORTED, STATUS_MAX_TURNS, STATUS_NO_DATA, STATUS_OK, STATUS_TIMEOUT,
    SubagentResult, SubagentTask,
)


class FakeResp:
    def __init__(self, content="", tool_calls=None, usage=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = usage or {}


class ScriptedLLM:
    """按脚本返回响应；脚本用完后重复最后一条。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.bound = None

    def invoke(self, messages):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[idx]

    def bind_tools(self, tools):
        self.bound = tools
        return self


CARD_OK = {"asof": "2026-10-30", "status": "ok", "confidence": "high",
           "items": [{"metric": "PB(LF)", "value": 0.68, "unit": "倍",
                      "source": "mx_data_query", "period": "2026-10-30"}]}


def _task(**kw):
    base = dict(label="估值", slot="valuation", goal="取板块 PB", output_schema=CARD_SCHEMA,
                tool_whitelist=("t1",))
    base.update(kw)
    return SubagentTask(**base)


# ── 执行器 ──
class TestExecutor:
    def test_uses_tracked_invoke_with_label_when_radar_logger(self, monkeypatch):
        """有 RadarLogger 时必须走 tracked_invoke，并带 deep-fetch:<slot> label。"""
        import logging

        import utils.llm_factory as lf
        from utils.logger import RadarLogger

        seen = {}

        def fake_tracked(llm, messages, logger, label="", **kw):
            seen["label"] = label
            return FakeResp('{"asof":"2026-10-30","status":"no_data","items":[]}')

        monkeypatch.setattr(lf, "tracked_invoke", fake_tracked)
        ex = SubagentExecutor(llm=ScriptedLLM([FakeResp("{}")]), tools={"t1": lambda: "x"},
                              log=RadarLogger(logging.getLogger("sub_test")))
        ex.run(_task())
        assert seen.get("label", "").startswith("deep-fetch:valuation")

    def test_tool_then_structured_ok(self):
        llm = ScriptedLLM([
            FakeResp("", [{"name": "t1", "args": {"q": "PB"}, "id": "1"}]),
            FakeResp('```json\n{"asof":"2026-10-30","status":"ok","items":[{"metric":"PB","value":0.68,"unit":"倍","source":"t1"}]}\n```'),
        ])
        ex = SubagentExecutor(llm=llm, tools={"t1": lambda q: "PB=0.68"})
        res = ex.run(_task())
        assert res.status == STATUS_OK and res.tool_calls == 1
        assert res.structured["items"][0]["value"] == 0.68

    def test_zero_tool_calls_is_no_data(self):
        llm = ScriptedLLM([FakeResp('{"asof":"2026-10-30","status":"no_data","items":[]}')])
        ex = SubagentExecutor(llm=llm, tools={"t1": lambda q: ""})
        res = ex.run(_task())
        assert res.status == STATUS_NO_DATA and res.tool_calls == 0

    def test_max_turns(self):
        llm = ScriptedLLM([FakeResp("", [{"name": "t1", "args": {}, "id": "1"}])])
        ex = SubagentExecutor(llm=llm, tools={"t1": lambda: "x"})
        res = ex.run(_task(max_turns=3))
        assert res.status in (STATUS_MAX_TURNS, STATUS_NO_DATA)
        assert res.turns == 3

    def test_timeout(self):
        llm = ScriptedLLM([FakeResp("", [{"name": "t1", "args": {}, "id": "1"}])])
        ex = SubagentExecutor(llm=llm, tools={"t1": lambda: "x"})
        res = ex.run(_task(deadline_s=0.0))
        assert res.status == STATUS_TIMEOUT

    def test_aborted(self):
        ev = threading.Event()
        ev.set()
        llm = ScriptedLLM([FakeResp("{}")])
        ex = SubagentExecutor(llm=llm, tools={})
        res = ex.run(_task(), cancel=ev)
        assert res.status == STATUS_ABORTED

    def test_tool_exception_does_not_raise(self):
        def boom(q):
            raise RuntimeError("tool down")

        llm = ScriptedLLM([
            FakeResp("", [{"name": "t1", "args": {}, "id": "1"}]),
            FakeResp('{"asof":"2026-10-30","status":"ok","items":[{"metric":"PB","value":0.7,"source":"t1"}]}'),
        ])
        res = SubagentExecutor(llm=llm, tools={"t1": boom}).run(_task())
        assert res.status == STATUS_OK and res.tool_calls == 1


# ── 取数链 ──
class StubExecutor:
    """按工具名返回预设结果。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def run(self, task, cancel=None):
        tool = task.tool_whitelist[0]
        self.calls.append(tool)
        val = self.mapping.get(tool)
        if isinstance(val, SubagentResult):
            return val
        if val is None:
            return SubagentResult(STATUS_NO_DATA, "no-data")
        return SubagentResult(STATUS_OK, "completed", structured=val, tool_calls=1, turns=1)


class TestFetch:
    def test_fallback_chain(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "rid1"))
        ex = StubExecutor({"mx_data_query": None,
                           "iwc_query": {"asof": "2026-10-30", "status": "ok",
                                         "items": [{"metric": "PB", "value": 0.7, "source": "iwc_query"}]}})
        card = FetchService(executor=ex, store=store, rid="rid1").fetch("valuation", "银行板块")
        assert card["status"] == "ok" and card["items"]
        assert ex.calls == ["mx_data_query", "iwc_query"]

    def test_all_fail_is_no_data(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "rid2"))
        ex = StubExecutor({})
        card = FetchService(executor=ex, store=store, rid="rid2").fetch("valuation", "银行板块")
        assert card["status"] == "no_data" and card["items"] == [] and card["gaps"]
        assert (store.dir_of("rid2") / "evidence" / f"{card['ev_id']}.json").exists()

    def test_no_executor_is_no_data(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "rid3"))
        card = FetchService(executor=None, store=store, rid="rid3").fetch("valuation", "银行板块")
        assert card["status"] == "no_data"

    def test_card_validation(self):
        assert validate_card({"asof": "2026-10-30", "items": [{"metric": "PB", "value": 1, "source": "s"}]})[0]
        assert not validate_card({"asof": "2026/10/30", "items": [{"metric": "PB", "value": 1, "source": "s"}]})[0]
        assert not validate_card({"asof": "2026-10-30", "items": [{"metric": "PB", "value": 1}]})[0]
        assert not validate_card({"asof": "2026-10-30", "items": []})[0]

    def test_summary_lines(self):
        ok = {"slot": "valuation", "status": "ok", "asof": "2026-10-30",
              "items": [{"metric": "PB(LF)", "value": 0.68, "unit": "倍", "source": "mx"}]}
        assert "PB(LF)：0.68倍（mx，2026-10-30）" in summary_lines(ok)[0]
        bad = {"slot": "valuation", "status": "no_data", "gaps": ["接口未返回"]}
        assert "数据缺失" in summary_lines(bad)[0]


# ── 图表（spec 优先 + 规则兜底） ──
class TestCharts:
    def test_fallback_chart_from_items(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "ridc"))
        ex = StubExecutor({"mx_data_query": {
            "asof": "2026-10-30", "status": "ok",
            "items": [{"metric": "PB", "value": 0.68, "unit": "倍", "source": "mx"},
                      {"metric": "ROE", "value": 8.6, "unit": "倍", "source": "mx"}],
        }})
        card = FetchService(executor=ex, store=store, rid="ridc").fetch("valuation", "银行板块")
        assert card["charts"], "同单位 ≥2 个数值时应规则兜底出一张图"
        ch = card["charts"][0]
        assert ch["type"] == "bar" and ch["asof"] == "2026-10-30"

    def test_no_fallback_when_units_differ(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "ridu"))
        ex = StubExecutor({"mx_data_query": {
            "asof": "2026-10-30", "status": "ok",
            "items": [{"metric": "点位", "value": 7962, "unit": "点", "source": "mx"},
                      {"metric": "成交额", "value": 412, "unit": "亿", "source": "mx"}],
        }})
        card = FetchService(executor=ex, store=store, rid="ridu").fetch("valuation", "银行板块")
        assert card["charts"] == [], "不同单位不得混画一张图"

    def test_spec_preferred_over_fallback(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "ridd"))
        spec = {"id": "c1", "type": "line", "title": "净息差", "source": "mx", "asof": "2026-10-30",
                "x": {"values": ["24Q1", "24Q2"]},
                "series": [{"name": "净息差", "data": [1.4, 1.5]}]}
        ex = StubExecutor({"mx_data_query": {
            "asof": "2026-10-30", "status": "ok",
            "items": [{"metric": "PB", "value": 0.68, "source": "mx"},
                      {"metric": "ROE", "value": 8.6, "source": "mx"}],
            "charts": [spec],
        }})
        card = FetchService(executor=ex, store=store, rid="ridd").fetch("valuation", "银行板块")
        assert [c["id"] for c in card["charts"]] == ["c1"]

    def test_unsourced_chart_dropped(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(_meta(store, "ride"))
        ex = StubExecutor({"mx_data_query": {
            "asof": "2026-10-30", "status": "ok",
            "items": [{"metric": "PB", "value": 0.68, "source": "mx"},
                      {"metric": "ROE", "value": 8.6, "source": "mx"}],
            "charts": [{"id": "c9", "type": "line", "title": "无源图",
                        "series": [{"name": "x", "data": [1]}], "asof": "2026-10-30"}],
        }})
        card = FetchService(executor=ex, store=store, rid="ride").fetch("valuation", "银行板块")
        assert all(c["id"] != "c9" for c in card["charts"])


# ── 注入位置（前缀缓存红线） ──
class TestInjection:
    def test_evidence_only_in_tail(self):
        outline = Outline(title="T", template_id="deep_sector_finance", variant="finance",
                          sections=[OutlineSection(1, "summary", "投资摘要")])
        sec = outline.sections[0]
        msg = _user_prompt(outline, sec, [], [{
            "slot": "valuation", "status": "ok", "asof": "2026-10-30", "items": [
                {"metric": "PB", "value": 0.68, "unit": "倍", "source": "mx"}], "gaps": ["北向"]}])
        assert "本节可用证据" in msg
        # 红线：证据与结论清单都在尾部（章节头之后），不得插进稳定前缀
        assert msg.index("章节：第 1 章") < msg.index("本节可用证据")
        assert msg.index("章节：第 1 章") < msg.index("已写章节结论清单")
        assert "{" not in msg  # 不得把原始 JSON 塞进去


def _meta(store, rid):
    from agents.deep_report.models import ReportMeta
    return ReportMeta(rid=rid, universe="银行板块", user_request="", template_id="deep_sector_finance",
                      variant="finance", status="draft")


class TestNormItemsNaN:
    def test_nan_values_dropped(self):
        from agents.deep_report.fetch import _norm_items
        gaps = []
        items = _norm_items({"items": [
            {"metric": "PB", "value": 0.6, "unit": "倍", "source": "x"},
            {"metric": "avg_cost", "value": float("nan"), "unit": "元", "source": "x"},
            {"metric": "x", "value": float("inf"), "unit": "", "source": "x"},
        ]}, dropped=gaps)
        assert len(items) == 1
        assert gaps and "丢弃 2 条" in gaps[0]

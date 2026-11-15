"""Step 2 单测：调度分配能力 → 按能力取数（不再写死厂商工具）。"""
from __future__ import annotations

import threading

from agents.deep_report import capabilities as caps
from agents.deep_report.fetch import FetchService
from agents.deep_report.models import ReportMeta
from agents.deep_report.runtime import DeepReportRuntime
from agents.deep_report.store import DeepReportStore
from agents.subagent.models import STATUS_OK, SubagentResult


def _card(slot, items=None):
    return {"asof": "2026-10-30", "status": "ok",
            "items": items or [{"metric": "PB", "value": 0.68, "unit": "倍", "source": "mx"}],
            "charts": [], "gaps": []}


class RecorderExecutor:
    def __init__(self, payload_by_slot=None):
        self.tasks = []
        self.payload = payload_by_slot or {}

    def run(self, task, cancel=None):
        self.tasks.append(task)
        payload = self.payload.get(task.slot)
        if payload is None:
            return SubagentResult("no_data", "no-data")
        return SubagentResult(STATUS_OK, "completed", structured=payload, tool_calls=1, turns=1)


class TestOutlinerCapabilities:
    def test_fallback_maps_slots_to_capabilities(self):
        from agents.deep_report.outliner import build_outline
        from agents.deep_report.templates import load_template
        tpl = load_template("deep_sector_finance")
        outline = build_outline(tpl, universe="银行板块", llm=None)
        summary = next(s for s in outline.sections if s.section_id == "summary")
        assert summary.capabilities == [caps.SNAPSHOT]
        # 没有数据槽的章节（目录）能力为空
        catalog = next(s for s in outline.sections if s.section_id == "catalog")
        assert catalog.capabilities == []


class TestFetchCapability:
    def test_uses_capability_agent_tools_not_vendor(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="r1", universe="银行板块", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        rec = RecorderExecutor({"snapshot": _card("snapshot")})
        card = FetchService(executor=rec, store=store, rid="r1").fetch_capability("snapshot", "银行板块")
        task = rec.tasks[0]
        assert task.slot == "snapshot"
        # 断言：白名单非空、且不含厂商 query 工具（真正的红线）
        assert task.tool_whitelist, "能力任务必须有可用技能工具"
        assert not (set(task.tool_whitelist) & {"mx_data_query", "iwc_query", "mx_search_news"})
        assert "get_stock_realtime" in task.tool_whitelist
        assert card["status"] == "ok" and card.get("capability") == "snapshot"

    def test_unknown_capability_is_no_data(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="r2", universe="x", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        card = FetchService(executor=RecorderExecutor(), store=store, rid="r2").fetch_capability("nope", "x")
        assert card["status"] == "no_data" and "未知能力" in "".join(card["gaps"])


class TestRuntimeCapabilityMode:
    def _run(self, tmp_path, use_capabilities: bool):
        store = DeepReportStore(tmp_path)
        rec = RecorderExecutor({"snapshot": _card("snapshot"), "consensus": _card("consensus"),
                                "valuation": _card("valuation")})
        rt = DeepReportRuntime(store=store, llm=None, executor=rec, fetch=True,
                               qa_enabled=False, semantic_review=False,
                               use_capabilities=use_capabilities)
        meta = rt.run(universe="银行板块", template_id="deep_sector_finance",
                      max_sections=3, cancel=threading.Event())
        return meta, [t.slot for t in rec.tasks]

    def test_capability_mode_uses_capability_names(self, tmp_path):
        meta, keys = self._run(tmp_path / "cap", True)
        assert meta["status"] in ("done", "partial")
        assert "consensus_poll" not in keys, f"能力模式不应再按旧槽派发: {keys}"
        assert set(keys) & set(caps.all_names()), keys

    def test_legacy_mode_uses_slots(self, tmp_path):
        _, keys = self._run(tmp_path / "legacy", False)
        # 旧槽模式应出现模板里的原始槽名（industry_space 是 overview 章节的槽）
        assert "industry_space" in keys and "consensus_poll" in keys


class SlowExecutor:
    def run(self, task, cancel=None):
        import time
        time.sleep(0.8)
        return SubagentResult(STATUS_OK, "completed", structured=_card("snapshot"))


class TestHardTimeout:
    def test_hard_timeout_returns_no_data(self, tmp_path, monkeypatch):
        """评审 P1-2：子代理卡住时必须在看门狗时间内收敛，不能拖死取数池。"""
        from agents.deep_report import fetch as fetch_mod
        monkeypatch.setattr(fetch_mod, "_EXEC_TIMEOUT_PAD_S", 0.05)
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="rto", universe="银行板块", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        svc = FetchService(executor=SlowExecutor(), store=store, rid="rto")
        card = svc.fetch_capability("snapshot", "银行板块", deadline_s=0.1)
        assert card["status"] == "no_data"
        assert "timeout" in "".join(card["gaps"])


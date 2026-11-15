"""多期序列接入取数的集成单测（全部离线）。

覆盖三件事：
1. 宽表 payload → 结论卡 items（带 period）+ 折线 spec；
2. 卡片 → `build_candidates` 真的产出 **series 候选**（折线候选不再恒为 0），
   且该候选能过 `compile_spec`（是能画出来的图）；
3. 新逻辑失败时**逐级降级**为原有单期行为（无 payload / 抛异常 / 期数不足 /
   非法 JSON / 超时 / 关掉开关），不新增 gaps、不改卡片 schema。
"""
from __future__ import annotations

import json
import time

import pytest

from agents.deep_report import fetch as fetch_mod
from agents.deep_report.chart_candidates import SHAPE_SERIES, build_candidates
from agents.deep_report.chart_compiler import compile_spec
from agents.deep_report.fetch import (
    SERIES_MAX_POINTS, SERIES_MIN_POINTS, SERIES_SLOTS, FetchService,
    call_with_timeout, invoke_tool, series_line_spec, summary_lines,
)
from agents.deep_report.store import DeepReportStore
from agents.subagent.models import STATUS_OK, SubagentResult

# ── 夹具：真实 iwc 宽表形态 ──
def _col(key: str, unit: str = "点") -> dict:
    return {"index_name": f"全量指数@{key}", "key": key, "feKey": key,
            "type": "DOUBLE", "unit": unit, "label": ""}


def _valid_days(n: int) -> list:
    """生成 n 个「日历合法」的 YYYYMMDD（避免 fixture 造出 20261000 这种假日期）。"""
    from datetime import date, timedelta
    last = date(2026, 10, 30)
    return [(last - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def wide_payload_valid(n: int = 60, metric: str = "收盘价", unit: str = "点",
                       base: float = 7000.0) -> str:
    days = _valid_days(n)
    cols = [_col("指数简称", ""), _col("最新价", "点")]
    cols += [_col(f"{metric}[{d}]", unit) for d in days]
    row = {"指数简称": "中证银行", "最新价": str(base + n)}
    for i, d in enumerate(days):
        row[f"{metric}[{d}]"] = base + i
    return json.dumps({
        "query": "中证银行指数近60个交易日收盘点位",
        "skill_id": "hithink-market-query",
        "data": {"columns": cols, "datas": [row]},
    }, ensure_ascii=False)


CARD_OK = {
    "asof": "2026-10-30", "status": "ok", "confidence": "high",
    "items": [{"metric": "最新价", "value": 7962.26, "unit": "点",
               "source": "iwc_query", "period": "2026-10-30"}],
}

# 序列指标的固定名字（fixture 的宽表列名），用它区分「新补的序列点」与原单期 item
SERIES_METRIC = "收盘价"


def _series_items(card) -> list:
    """卡片里新补的序列点（注意：原单期 item 自带 period，不能按 period 判）。"""
    return [it for it in card["items"] if it["metric"] == SERIES_METRIC]


def _periods(card) -> list:
    """序列点（带 period 的）——锚点 period 为空，不算期数。"""
    return [it["period"] for it in _series_items(card) if it["period"]]


class StubExecutor:
    """按工具名返回预设结论卡；`tools` 缺省不存在 → 序列逻辑应自动降级。"""

    def __init__(self, mapping=None, tools=None):
        self.mapping = dict(mapping or {"iwc_query": CARD_OK, "mx_data_query": None})
        self.calls = []
        if tools is not None:
            self.tools = tools

    def run(self, task, cancel=None):
        tool = task.tool_whitelist[0]
        self.calls.append(tool)
        val = self.mapping.get(tool)
        if isinstance(val, SubagentResult):
            return val
        if val is None:
            return SubagentResult("no_data", "no-data")
        return SubagentResult(STATUS_OK, "completed", structured=val, tool_calls=1, turns=1)


def _meta(store, rid):
    from agents.deep_report.models import ReportMeta
    return ReportMeta(rid=rid, universe="中证银行指数(399986.SZ)", user_request="",
                      template_id="deep_sector_finance", variant="finance", status="draft")


def _svc(tmp_path, rid, *, mapping=None, tools=None, fetcher=None, enabled=True):
    store = DeepReportStore(tmp_path)
    store.create(_meta(store, rid))
    ex = StubExecutor(mapping=mapping, tools=tools)
    svc = FetchService(executor=ex, store=store, rid=rid,
                       series_fetcher=fetcher, series_enabled=enabled)
    return svc, ex, store


# ── ① 宽表 → 卡片 ──
class TestAttachSeries:
    def test_snapshot_card_gets_multi_period_items(self, tmp_path):
        asked = []

        def fetcher(tool, query):
            asked.append((tool, query))
            return wide_payload_valid(60)

        svc, ex, store = _svc(tmp_path, "r1", fetcher=fetcher)
        card = svc.fetch("snapshot", "中证银行指数(399986.SZ)")

        assert card["status"] == "ok"
        periods = _periods(card)
        assert len(periods) >= 4, "同一 metric 必须多期，series 形状才成立"
        assert len(set(periods)) == len(periods), "同一日期不应重复"
        assert asked and asked[0][0] == "iwc_query", "用取数成功的那个工具"
        assert "中证银行指数(399986.SZ)" in asked[0][1], "问句要带标的"

    def test_query_uses_proven_period_phrasing(self, tmp_path):
        """模板必须是实测能出宽表的问法（成交额要带「每日/收盘点位」）。"""
        asked = []
        svc, _ex, _st = _svc(tmp_path, "r2",
                             fetcher=lambda t, q: asked.append(q) or wide_payload_valid(6))
        svc.fetch("snapshot", "银行板块")
        assert "收盘点位" in asked[0]

    def test_valuation_slot_also_gets_series(self, tmp_path):
        card_ok = {"asof": "2026-09-30", "status": "ok", "items": [
            {"metric": "PB(LF)", "value": 0.64, "unit": "倍", "source": "mx_data_query"}]}
        svc, _ex, _st = _svc(tmp_path, "r3",
                             mapping={"iwc_query": card_ok, "mx_data_query": None},
                             fetcher=lambda t, q: wide_payload_valid(12, "市净率", ""))
        card = svc.fetch("valuation", "银行板块")
        assert any(it["metric"] == "市净率" for it in card["items"])
        assert len([it for it in card["items"]
                    if it["metric"] == "市净率" and it["period"]]) == 12

    def test_items_keep_schema_contract(self, tmp_path):
        svc, _ex, _st = _svc(tmp_path, "r4", fetcher=lambda t, q: wide_payload_valid(8))
        card = svc.fetch("snapshot", "银行板块")
        for it in card["items"]:
            assert it["metric"] and it["source"], "metric/source 为空会被 _norm_items 丢掉"
            assert isinstance(it["value"], (int, float))
            assert "unit" in it and "period" in it

    def test_card_schema_unchanged(self, tmp_path):
        svc, _ex, _st = _svc(tmp_path, "r5", fetcher=lambda t, q: wide_payload_valid(8))
        card = svc.fetch("snapshot", "银行板块")
        assert set(card) == {"ev_id", "slot", "asof", "status", "items", "charts",
                             "gaps", "confidence", "tool_calls", "turns"}
        assert card["gaps"] == [], "有序列不算缺口，不得往 gaps 里塞"

    def test_anchor_item_is_current_value_first(self, tmp_path):
        """第一条同指标 items 必须是「当期值」（QA/summary 只认第一条）。"""
        svc, _ex, _st = _svc(tmp_path, "r6",
                             fetcher=lambda t, q: wide_payload_valid(6, base=100.0))
        card = svc.fetch("snapshot", "银行板块")
        same = [it for it in card["items"] if it["metric"] == "收盘价"]
        assert same[0]["period"] == "", "锚点不带 period（否则折线 x 轴会跳）"
        series = [it for it in same if it["period"]]
        latest = max(series, key=lambda it: it["period"])
        assert same[0]["value"] == latest["value"], "锚点应是序列最新一期"

    def test_series_line_spec_attached_when_no_llm_chart(self, tmp_path):
        svc, _ex, _st = _svc(tmp_path, "r7", fetcher=lambda t, q: wide_payload_valid(30))
        card = svc.fetch("snapshot", "银行板块")
        assert len(card["charts"]) == 1
        spec = card["charts"][0]
        assert spec["type"] == "line"
        assert spec["source"] and spec["asof"]
        assert len(spec["x"]["values"]) == len(spec["series"][0]["data"]) == 30
        assert compile_spec(spec)["series"][0]["type"] == "line"

    def test_llm_charts_win_over_generated_line(self, tmp_path):
        spec = {"id": "c1", "type": "line", "title": "净息差", "source": "mx",
                "asof": "2026-10-30", "x": {"values": ["24Q1", "24Q2"]},
                "series": [{"name": "净息差", "data": [1.4, 1.5]}]}
        card_ok = dict(CARD_OK, charts=[spec])
        svc, _ex, _st = _svc(tmp_path, "r8",
                             mapping={"iwc_query": card_ok, "mx_data_query": None},
                             fetcher=lambda t, q: wide_payload_valid(20))
        card = svc.fetch("snapshot", "银行板块")
        assert [c["id"] for c in card["charts"]] == ["c1"], "LLM 给的 spec 优先"

    def test_points_capped(self, tmp_path):
        svc, _ex, _st = _svc(tmp_path, "r9",
                             fetcher=lambda t, q: wide_payload_valid(SERIES_MAX_POINTS + 40))
        card = svc.fetch("snapshot", "银行板块")
        assert len(_periods(card)) == SERIES_MAX_POINTS
        assert card["charts"][0]["x"]["values"][-1] == "2026-10-30", "截断只砍最老的"

    def test_persisted_card_has_series(self, tmp_path):
        svc, _ex, store = _svc(tmp_path, "r10", fetcher=lambda t, q: wide_payload_valid(6))
        card = svc.fetch("snapshot", "银行板块")
        saved = json.loads((store.dir_of("r10") / "evidence" / f"{card['ev_id']}.json")
                           .read_text(encoding="utf-8"))
        assert len([it for it in saved["items"]
                    if it["metric"] == SERIES_METRIC and it["period"]]) == 6


# ── ② 卡片 → 折线候选（任务的核心验收点） ──
class TestSeriesCandidate:
    def _card(self, tmp_path, rid="rc1", n=30):
        svc, _ex, _st = _svc(tmp_path, rid, fetcher=lambda t, q: wide_payload_valid(n))
        return svc.fetch("snapshot", "中证银行指数(399986.SZ)")

    def test_build_candidates_produces_series_shape(self, tmp_path):
        card = self._card(tmp_path)
        cands = build_candidates({"snapshot": card})
        series = [c for c in cands if c["shape"] == SHAPE_SERIES]
        assert series, "宽表序列卡片必须产出折线候选（此前恒为 0）"
        line = series[0]
        assert line["spec"]["type"] == "line"
        # ≥4 期就成立；30 期 fixture 应完整映射到 x/series
        assert line["size"] == 30
        assert len(line["spec"]["x"]["values"]) == 30
        assert len(line["spec"]["series"][0]["data"]) == 30

    def test_series_candidate_compiles(self, tmp_path):
        card = self._card(tmp_path, "rc2", n=8)
        cands = [c for c in build_candidates({"snapshot": card})
                 if c["shape"] == SHAPE_SERIES]
        option = compile_spec(cands[0]["spec"])
        assert option["xAxis"]["type"] == "category" and len(option["xAxis"]["data"]) == 8
        assert option["series"][0]["type"] == "line"

    def test_series_outranks_cross_section(self, tmp_path):
        card = self._card(tmp_path, "rc3", n=10)
        cands = build_candidates({"snapshot": card})
        assert cands[0]["shape"] == SHAPE_SERIES, "折线优先级应高于横截面柱"

    def test_single_period_card_has_no_series(self, tmp_path):
        """回归护栏：老的单期卡片仍然出不了折线（形状不够就不画）。"""
        svc, _ex, _st = _svc(tmp_path, "rc4", fetcher=None)
        card = svc.fetch("snapshot", "银行板块")
        shapes = {c["shape"] for c in build_candidates({"snapshot": card})}
        assert SHAPE_SERIES not in shapes

    def test_below_min_points_no_series(self, tmp_path):
        svc, _ex, _st = _svc(tmp_path, "rc5",
                             fetcher=lambda t, q: wide_payload_valid(SERIES_MIN_POINTS - 1))
        card = svc.fetch("snapshot", "银行板块")
        assert _series_items(card) == [], "不足 4 期不接序列"
        assert all(c["shape"] != SHAPE_SERIES
                   for c in build_candidates({"snapshot": card}))


# ── ③ 降级（失败必须退回原有单期行为） ──
class TestDegrade:
    def _assert_degrades(self, tmp_path, rid, fetcher):
        """降级 = 与「关掉序列开关」时逐字段相同（不新增 items/charts/gaps）。"""
        base_svc, _bx, _bs = _svc(tmp_path, f"{rid}_base", enabled=False)
        base = base_svc.fetch("snapshot", "银行板块")
        svc, _ex, _st = _svc(tmp_path, rid, fetcher=fetcher)
        card = svc.fetch("snapshot", "银行板块")
        assert card["items"] == base["items"]
        assert card["charts"] == base["charts"]
        assert card["status"] == base["status"] and card["gaps"] == base["gaps"]
        return card

    def test_no_payload(self, tmp_path):
        card = self._assert_degrades(tmp_path, "d1", lambda t, q: None)
        assert [it["metric"] for it in card["items"]] == ["最新价"], "只剩原单期 items"

    def test_fetcher_raises(self, tmp_path):
        def boom(tool, query):
            raise RuntimeError("tool down")

        self._assert_degrades(tmp_path, "d2", boom)

    def test_invalid_json(self, tmp_path):
        self._assert_degrades(tmp_path, "d3", lambda t, q: "<html>502</html>")

    def test_no_series_columns(self, tmp_path):
        """工具返回了数据但没有日期列（只有单期快照）。"""
        snap = json.dumps({"columns": [_col("最新价")], "datas": [{"最新价": "7962.26"}]},
                          ensure_ascii=False)
        self._assert_degrades(tmp_path, "d4", lambda t, q: snap)

    def test_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch_mod, "SERIES_TIMEOUT_S", 0.05)

        def slow(tool, query):
            time.sleep(0.25)
            return wide_payload_valid(60)

        self._assert_degrades(tmp_path, "d5", slow)

    def test_switch_off(self, tmp_path):
        called = []
        svc, _ex, _st = _svc(tmp_path, "d6", enabled=False,
                             fetcher=lambda t, q: called.append(1) or wide_payload_valid(30))
        card = svc.fetch("snapshot", "银行板块")
        assert called == [], "关掉开关就不该发第二次请求"
        assert _series_items(card) == []

    def test_non_series_slot_untouched(self, tmp_path):
        called = []
        svc, _ex, _st = _svc(tmp_path, "d7",
                             fetcher=lambda t, q: called.append(1) or wide_payload_valid(30))
        svc.fetch("capital_flow", "银行板块")
        assert called == []

    def test_executor_without_tools_degrades(self, tmp_path):
        """生产里走 executor.tools；StubExecutor 没有 tools → 直接跳过，不发请求。"""
        svc, ex, _st = _svc(tmp_path, "d8")
        card = svc.fetch("snapshot", "银行板块")
        assert ex.calls == ["mx_data_query", "iwc_query"], "仍是原降级链"
        assert _series_items(card) == []

    def test_series_slots_registry(self):
        assert "snapshot" in SERIES_SLOTS and "valuation" in SERIES_SLOTS


# ── ④ 工具调用适配（executor.tools 的真实形态） ──
class FakeStructuredTool:
    """模拟 LangChain StructuredTool：只认 `query`（多传 limit 会报错）。"""

    def __init__(self, payload):
        self.payload = payload
        self.args = []

    def invoke(self, args):
        if set(args) - {"query"}:
            raise ValueError(f"unexpected args: {sorted(args)}")
        self.args.append(args)
        return self.payload


class TestToolInvocation:
    def test_invoke_retries_without_optional_args(self):
        tool = FakeStructuredTool(wide_payload_valid(5))
        out = invoke_tool(tool, "银行板块近60个交易日收盘点位")
        assert out and tool.args == [{"query": "银行板块近60个交易日收盘点位"}]

    def test_invoke_plain_callable(self):
        assert invoke_tool(lambda query: "ok:" + query, "q1") == "ok:q1"

    def test_invoke_none_when_everything_fails(self):
        def bad(**kw):
            raise RuntimeError("nope")

        assert invoke_tool(bad, "q") is None

    def test_executor_tools_used(self, tmp_path):
        tool = FakeStructuredTool(wide_payload_valid(25))
        svc, _ex, _st = _svc(tmp_path, "t1", tools={"iwc_query": tool,
                                                    "mx_data_query": tool})
        card = svc.fetch("snapshot", "中证银行指数(399986.SZ)")
        assert len(_periods(card)) == 25
        assert tool.args and "收盘点位" in tool.args[0]["query"]

    def test_call_with_timeout_returns_value(self):
        assert call_with_timeout(lambda: 42, 1.0) == 42

    def test_call_with_timeout_raises_original_error(self):
        def boom():
            raise RuntimeError("inner")

        with pytest.raises(RuntimeError, match="inner"):
            call_with_timeout(boom, 1.0)

    def test_call_with_timeout_gives_up(self):
        with pytest.raises(TimeoutError):
            call_with_timeout(lambda: time.sleep(0.3), 0.05)


# ── ⑤ 摘要行口径 ──
class TestSummaryLines:
    def test_series_points_show_own_period(self):
        card = {"slot": "snapshot", "status": "ok", "asof": "2026-10-30", "items": [
            {"metric": "收盘价", "value": 7918.19, "unit": "点", "source": "iwc",
             "period": "2026-10-29"}]}
        line = summary_lines(card)[0]
        assert "2026-10-29" in line, "多期点必须标自己的日期，不能一律标 asof"
        assert "2026-10-30" not in line

    def test_single_period_falls_back_to_asof(self):
        card = {"slot": "snapshot", "status": "ok", "asof": "2026-10-30", "items": [
            {"metric": "点位", "value": 1.0, "unit": "点", "source": "iwc"}]}
        assert "2026-10-30" in summary_lines(card)[0]

    def test_line_spec_title_and_span(self):
        series = [{"metric": "收盘价", "value": 1.0 + i, "unit": "点",
                   "source": "iwc_query", "period": f"2026-10-{10 + i:02d}"}
                  for i in range(4)]
        spec = series_line_spec(series, ev_id="ev_001", asof="2026-10-13")
        assert spec["id"] == "ser_ev_001"
        assert "2026-10-10~2026-10-13" in spec["title"]
        assert spec["asof"] == "2026-10-13"

    def test_line_spec_falls_back_to_last_period_asof(self):
        series = [{"metric": "收盘价", "value": 1.0, "unit": "点",
                   "source": "iwc_query", "period": "2026-10-13"}]
        spec = series_line_spec(series, ev_id="ev_002", asof="")
        assert spec["asof"] == "2026-10-13", "asof 非法时用最后一期，不能留空"

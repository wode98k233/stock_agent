"""图表候选与选择单测（纯逻辑，不调 LLM）。"""
from __future__ import annotations

from agents.deep_report.chart_candidates import (
    MAX_TOTAL_CHARTS, SHAPE_CROSS, SHAPE_DIST, SHAPE_SERIES, SHAPE_SHARE,
    build_candidates, select,
)


def _card(ev="ev_001", slot="valuation", items=None, charts=None):
    return {"ev_id": ev, "slot": slot, "status": "ok", "asof": "2026-10-30",
            "items": items or [], "charts": charts or [], "gaps": []}


def _series_items():
    return [{"metric": "净息差", "value": 1.4 + i * 0.01, "unit": "%",
             "source": "mx", "period": f"20{20 + i}Q1"} for i in range(6)]


def _cross_items():
    return [{"metric": n, "value": v, "unit": "倍", "source": "mx", "period": "2026-10-30"}
            for n, v in [("工行", 0.7), ("建行", 0.68), ("招行", 1.1), ("兴业", 0.55)]]


def _share_items():
    return [{"metric": n, "value": v, "unit": "%", "source": "mx", "period": "2026-10-30"}
            for n, v in [("大行", 45), ("股份行", 30), ("城商行", 20), ("农商行", 5)]]


class TestCandidates:
    def test_series_shape(self):
        cands = build_candidates({"valuation": _card(items=_series_items())})
        shapes = {c["shape"] for c in cands}
        assert SHAPE_SERIES in shapes
        line = [c for c in cands if c["shape"] == SHAPE_SERIES][0]
        assert line["spec"]["type"] == "line" and len(line["spec"]["series"][0]["data"]) == 6

    def test_cross_section_shape(self):
        cands = build_candidates({"capital": _card(slot="capital", items=_cross_items())})
        assert any(c["shape"] == SHAPE_CROSS for c in cands)

    def test_share_shape(self):
        cands = build_candidates({"structure": _card(slot="structure", items=_share_items())})
        assert any(c["shape"] == SHAPE_SHARE for c in cands)

    def test_distribution_needs_many_samples(self):
        many = [{"metric": f"股{i}", "value": i * 0.1, "unit": "倍", "source": "mx",
                 "period": "2026-10-30"} for i in range(10)]
        cands = build_candidates({"constituents": _card(slot="constituents", items=many)})
        assert any(c["shape"] == SHAPE_DIST for c in cands)

    def test_no_data_no_candidates(self):
        assert build_candidates({"x": _card(items=[])}) == []

    def test_candidates_have_source_and_asof(self):
        for c in build_candidates({"valuation": _card(items=_cross_items())}):
            assert c["spec"]["source"] and c["spec"]["asof"]

    def test_series_items_not_misread_as_cross_section(self):
        """多期 items 只该产 series 候选，不该被当成横截面/分布（否则出重复假图）。"""
        items = [{"metric": "收盘价", "value": 7000.0 + i, "unit": "点", "source": "mx",
                  "period": f"2026-10-{i + 1:02d}"} for i in range(10)]
        items.append({"metric": "收盘价", "value": 7010.0, "unit": "点", "source": "mx", "period": ""})
        cands = build_candidates({"snapshot": _card(items=items)})
        shapes = {c["shape"] for c in cands}
        assert SHAPE_SERIES in shapes
        assert SHAPE_CROSS not in shapes and SHAPE_DIST not in shapes


class TestSelect:
    def test_fallback_at_most_two_per_slot(self):
        cards = {"valuation": _card(items=_cross_items()),
                 "capital": _card(ev="ev_002", slot="capital", items=_share_items())}
        cands = build_candidates(cards)
        ids = select(cands, llm=None)
        slots = [c["slot"] for c in cands if c["id"] in ids]
        # 2026-11-02 起：同一槽允许 2 张（多股对比 + 分布），但不超过 2
        for slot in set(slots):
            assert slots.count(slot) <= 2, f"{slot} 超过 2 张"

    def test_cap_total(self):
        many = {}
        for i in range(20):
            many[f"s{i}"] = _card(ev=f"ev_{i:03d}", slot=f"s{i}", items=_cross_items())
        cands = build_candidates(many)
        assert len(select(cands, llm=None)) <= MAX_TOTAL_CHARTS

    def test_empty(self):
        assert select([], llm=None) == []

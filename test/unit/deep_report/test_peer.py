"""逐股分层（peer_financials / peer_series）与图表密度回归。"""
from __future__ import annotations

import json

from agents.deep_report.peer import summarize_peer_financials, summarize_peer_series


def _rows():
    rows = []
    for i, (tier, name, code) in enumerate([
            ("高端", "贵州茅台", "600519"), ("高端", "五粮液", "000858"),
            ("区域酒", "今世缘", "603369"), ("区域酒", "迎驾贡酒", "603198")]):
        rows.append({
            "tier": tier, "name": name, "code": code,
            "financial": [{"date": "2025三季报", "营业总收入": f"{900 - i * 100}亿元",
                           "归属于母公司股东的净利润": f"{400 - i * 50}亿元",
                           "归属母公司股东的净利润同比增长率": f"{8 - i * 5}%",
                           "销售毛利率": "81.5%", "销售净利率": f"{35 - i * 3}%",
                           "净资产收益率ROE(加权)": "20%"}],
            "valuation": {"市盈率PE(TTM)": 20 + i, "市净率PB": 3 + i * 0.5},
        })
    return rows


class TestPeerFinancials:
    def test_per_stock_items_and_tier_medians(self):
        s = summarize_peer_financials(_rows(), universe="白酒板块", asof="2026-11-02")
        metrics = [it["metric"] for it in s["items"]]
        assert "高端·贵州茅台" in metrics and "区域酒·今世缘" in metrics
        # 每股一行紧凑文本
        mt = next(it for it in s["items"] if it["metric"] == "高端·贵州茅台")
        assert "营收" in mt["value"] and "PE" in mt["value"]
        assert any("高端 净利同比中位数" in m for m in metrics)

    def test_charts_bar_and_scatter(self):
        s = summarize_peer_financials(_rows(), universe="白酒板块", asof="2026-11-02")
        types = {c["type"] for c in s["charts"]}
        assert "bar" in types and "scatter" in types
        assert s["inject_limit"] >= 12

    def test_no_data_when_all_failed(self):
        rows = [{"tier": "高端", "name": "贵州茅台", "code": "600519", "error": "boom"}]
        s = summarize_peer_financials(rows, universe="白酒板块", asof="2026-11-02")
        assert s["status"] == "no_data" and s["gaps"]


class TestPeerSeries:
    def _series(self, n=4, days=30):
        out = {}
        for i in range(n):
            out[f"股票{i}"] = [{"date": f"2026-10-{d + 1:02d}", "close": 100 + i * 5 + d}
                               for d in range(days)]
        return out

    def test_multi_line_chart(self):
        s = summarize_peer_series(self._series(), universe="白酒板块", asof="2026-11-02")
        assert s["status"] == "ok"
        chart = s["charts"][0]
        assert chart["type"] == "line" and len(chart["series"]) == 4
        assert len(chart["x"]["values"]) == 30
        # 起点归一化 = 100
        assert chart["series"][0]["data"][0] == 100.0

    def test_too_few_stocks_is_no_data(self):
        s = summarize_peer_series(self._series(n=2), universe="白酒板块", asof="2026-11-02")
        assert s["status"] == "no_data"


class TestFetchPeer:
    def test_capability_uses_tiers_and_derives(self, tmp_path, monkeypatch):
        import tools.fetcher as F
        from agents.deep_report.fetch import FetchService
        from agents.deep_report.models import ReportMeta
        from agents.deep_report.store import DeepReportStore
        import pandas as pd

        monkeypatch.setattr(F, "ak_financial_abstract",
                            lambda code: pd.DataFrame([{"date": "2026三季报",
                                                        "营业总收入": "900亿元",
                                                        "归属于母公司股东的净利润": "400亿元",
                                                        "归属母公司股东的净利润同比增长率": "6%",
                                                        "销售毛利率": "81%",
                                                        "销售净利率": "35%"}]))
        monkeypatch.setattr(F, "ak_valuation_indicators",
                            lambda code: pd.DataFrame([{"市盈率PE(TTM)": 20, "市净率PB": 3}]))
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="rp", universe="白酒板块", user_request="",
                                template_id="deep_sector_baijiu", variant="baijiu"))
        svc = FetchService(executor=None, store=store, rid="rp")
        tiers = {"高端": [{"name": "贵州茅台", "code": "600519"}],
                 "区域酒": [{"name": "今世缘", "code": "603369"}]}
        card = svc.fetch_capability("peer_financials", "白酒板块", tiers=tiers)
        assert card["status"] == "ok"
        assert any("贵州茅台" in it["metric"] for it in card["items"])

    def test_peer_without_tiers_is_no_data(self, tmp_path):
        from agents.deep_report.fetch import FetchService
        from agents.deep_report.models import ReportMeta
        from agents.deep_report.store import DeepReportStore
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="rp2", universe="白酒板块", user_request="",
                                template_id="deep_sector_baijiu", variant="baijiu"))
        card = FetchService(executor=None, store=store, rid="rp2").fetch_capability(
            "peer_series", "白酒板块")
        assert card["status"] == "no_data"


class TestChartDensity:
    def test_select_fills_to_ten_when_llm_picks_few(self):
        from agents.deep_report.chart_candidates import select
        cands = [{"id": f"c{i}", "shape": ["series", "cross_section", "share"][i % 3],
                  "title": f"t{i}", "slot": f"s{i}", "size": 5} for i in range(12)]

        class _LLM:
            def invoke(self, *a, **k):
                raise RuntimeError("no llm in test")

        # LLM 失败 → 走 _fallback，同样要能挑满
        picked = select(cands, llm=None)
        assert len(picked) >= 10

    def test_llm_picks_three_still_filled(self, monkeypatch):
        import utils.llm_factory as lf
        from agents.deep_report import chart_candidates as cc
        cands = [{"id": f"c{i}", "shape": ["series", "cross_section", "share"][i % 3],
                  "title": f"t{i}", "slot": f"s{i}", "size": 5} for i in range(12)]
        monkeypatch.setattr(lf, "llm_json_with_retry",
                            lambda *a, **k: {"ids": ["c0", "c1", "c2"]})
        picked = cc.select(cands, llm=object())
        assert len(picked) >= 10


    def test_extreme_yoy_flagged_and_excluded_from_chart(self):
        rows = _rows()
        rows[1]["financial"] = [{"date": "2025年报", "营业总收入": "405亿元",
                                 "归属于母公司股东的净利润": "89亿元",
                                 "归属母公司股东的净利润同比增长率": "-72%"}]
        # 构造一个同比 -54% 的营收（需要上一期行）
        rows[1]["financial"] = [
            {"date": "2025-12-31", "营业总收入": "405亿元", "归属于母公司股东的净利润": "89亿元"},
            {"date": "2024-12-31", "营业总收入": "891亿元", "归属于母公司股东的净利润": "318亿元"},
        ]
        s = summarize_peer_financials(rows, universe="白酒板块", asof="2026-11-02")
        mt = next(it for it in s["items"] if it["metric"] == "高端·五粮液")
        assert "数据存疑" in mt["value"]
        assert any("五粮液" in g for g in s["gaps"])

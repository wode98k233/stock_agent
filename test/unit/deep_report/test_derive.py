"""板块估值自算单测（纯函数 + 注入 fetcher，离线）。"""
from __future__ import annotations

from agents.deep_report.derive import (summarize_board_valuation,
                                       summarize_consensus, summarize_constituents)
from agents.deep_report.fetch import FetchService
from agents.deep_report.models import ReportMeta
from agents.deep_report.store import DeepReportStore
from agents.subagent.models import STATUS_NO_DATA, SubagentResult


def _rows(n=12):
    out = []
    for i in range(n):
        out.append({
            "代码": f"6000{i:02d}",
            "名称": f"银行{i}",
            "市净率(倍)": str(0.4 + i * 0.05),
            "市盈率(动)(倍)": str(4.0 + i * 0.3),
            "总市值(元)": f"{100 + i * 10}亿",
        })
    return out


class TestSummarize:
    def test_median_and_weighted(self):
        s = summarize_board_valuation(_rows(10), board_name="银行", asof="2026-10-30")
        assert s["status"] == "ok"
        metrics = {it["metric"]: it["value"] for it in s["items"]}
        assert "银行 PB 中位数" in metrics and "银行 PB(市值加权)" in metrics
        assert metrics["样本数"] == 10
        # PB 序列 0.40..0.85（10 个）中位数 = (0.60+0.65)/2 = 0.625
        assert abs(metrics["银行 PB 中位数"] - 0.625) < 1e-6

    def test_negative_pe_dropped(self):
        rows = _rows(6)
        rows[0]["市盈率(动)(倍)"] = "-5"
        s = summarize_board_valuation(rows, board_name="银行", asof="2026-10-30")
        assert s["status"] == "ok"
        assert not any("PE" in it["metric"] and it["value"] < 0 for it in s["items"])

    def test_distribution_chart_for_many_stocks(self):
        s = summarize_board_valuation(_rows(12), board_name="银行", asof="2026-10-30")
        assert s["charts"] and s["charts"][0]["type"] == "histogram"

    def test_no_pb_is_no_data(self):
        rows = [{"代码": "1", "名称": "x", "市净率(倍)": "-"}]
        s = summarize_board_valuation(rows, board_name="银行", asof="2026-10-30")
        assert s["status"] == "no_data" and s["gaps"]

    def test_low_sample_confidence(self):
        s = summarize_board_valuation(_rows(3), board_name="银行", asof="2026-10-30")
        assert s["confidence"] == "low" and s["gaps"]


class TestConsensus:
    def _rows(self, n=6):
        return [{"org": f"券商{i}", "author": "分析师", "title": f"银行行业深度{i}",
                 "date": f"2026-10-{10 + i:02d}", "url": "http://x",
                 "summary": "维持买入评级，目标价 12.5 元", "rating": "买入", "target": "12.5"}
                for i in range(n)]

    def test_rating_distribution_and_target(self):
        s = summarize_consensus(self._rows(4), universe="银行板块", asof="2026-10-30")
        metrics = {it["metric"]: it["value"] for it in s["items"]}
        assert s["status"] == "ok"
        assert metrics["近期研报数"] == 4 and metrics["覆盖机构数"] == 4
        assert metrics["评级·买入"] == 4
        assert metrics["目标价中位数"] == 12.5
        assert any("券商" in k for k in metrics), "近期观点要带机构与日期"

    def test_pie_chart_present(self):
        s = summarize_consensus(self._rows(3), universe="银行板块", asof="2026-10-30")
        assert s["charts"] and s["charts"][0]["type"] == "pie"

    def test_single_target_not_reported(self):
        rows = self._rows(2)
        rows[0]["target"] = ""      # 只剩 1 个目标价 → 不给中位数
        s = summarize_consensus(rows, universe="银行板块", asof="2026-10-30")
        metrics = {it["metric"]: it["value"] for it in s["items"]}
        assert "目标价中位数" not in metrics

    def test_empty_is_no_data(self):
        s = summarize_consensus([], universe="银行板块", asof="2026-10-30")
        assert s["status"] == "no_data" and s["gaps"]


class TestConsensusFallback:
    def test_fetch_capability_consensus_derives(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="r3", universe="银行板块", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        svc = FetchService(executor=NoDataExecutor(), store=store, rid="r3",
                           consensus_fetcher=lambda kw: [
                               {"org": "某证券", "title": "银行深度", "date": "2026-10-28",
                                "summary": "增持", "rating": "买入", "target": "11.2"}])
        card = svc.fetch_capability("consensus", "银行板块")
        assert card["status"] == "ok" and card["items"]
        assert card["charts"] and card["charts"][0]["type"] == "pie"
    def run(self, task, cancel=None):
        return SubagentResult(STATUS_NO_DATA, "no-data")


class NoDataExecutor:
    def run(self, task, cancel=None):
        return SubagentResult(STATUS_NO_DATA, "no-data")


class TestFetchFallback:
    def test_valuation_falls_back_to_derive(self, tmp_path):
        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="r1", universe="银行板块", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        svc = FetchService(executor=NoDataExecutor(), store=store, rid="r1",
                           derive_fetcher=lambda name: _rows(12))
        card = svc.fetch_capability("valuation", "银行板块",
                                    candidates=[{"code": "BK1283", "name": "银行", "kind": "board"}])
        assert card["status"] == "ok" and card["items"]
        assert card["charts"], "自算路径应带分布图"

    def test_derive_failure_degrades_to_no_data(self, tmp_path):
        def boom(_):
            raise RuntimeError("down")

        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="r2", universe="银行板块", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        svc = FetchService(executor=NoDataExecutor(), store=store, rid="r2", derive_fetcher=boom)
        card = svc.fetch_capability("valuation", "银行板块")
        assert card["status"] == "no_data"


    def test_chart_spec_compiles_with_matching_axis(self):
        """评审 P1-4：分布图的 x 轴必须是真实分箱，且与 series 等长。"""
        from agents.deep_report.chart_compiler import compile_spec
        s = summarize_board_valuation(_rows(12), board_name="银行", asof="2026-10-30")
        spec = s["charts"][0]
        opt = compile_spec(spec)
        assert len(opt["xAxis"]["data"]) == len(opt["series"][0]["data"])
        assert len(opt["xAxis"]["data"]) >= 2


    def test_stale_reports_add_gap_and_lower_confidence(self):
        rows = [{"org": "A", "title": "t", "date": "2025-01-01", "summary": "买入",
                 "rating": "买入", "target": "", "stale": True} for _ in range(9)]
        s = summarize_consensus(rows, universe="银行板块", asof="2026-11-02")
        assert s["gaps"] and "研报较旧" in s["gaps"][0]
        assert s["confidence"] == "low"


class TestConstituents:
    def _rows(self, n=12):
        out = []
        for i in range(n):
            out.append({"代码": f"6000{i:02d}", "名称": f"银行{i}",
                        "涨跌幅(%)": str((i - 6) * 0.5),
                        "市净率(倍)": str(0.5 + i * 0.02),
                        "总市值(元)": f"{100 + i * 10}亿"})
        return out

    def test_constituents_summary(self):
        s = summarize_constituents(self._rows(12), board_name="银行", asof="2026-11-02")
        metrics = {it["metric"]: it["value"] for it in s["items"]}
        assert s["status"] == "ok"
        assert metrics["银行 成分股数"] == 12
        assert "涨跌幅中位数" in metrics and "上涨/下跌家数" in metrics
        assert "PB 中位数" in metrics
        assert s["confidence"] == "medium"
        assert s["charts"] and s["charts"][0]["type"] == "bar"

    def test_constituents_no_rows(self):
        s = summarize_constituents([], board_name="银行", asof="2026-11-02")
        assert s["status"] == "no_data" and s["gaps"]

    def test_fetch_capability_constituents_falls_back_to_derive(self, tmp_path):
        from agents.deep_report.fetch import FetchService
        from agents.deep_report.models import ReportMeta
        from agents.deep_report.store import DeepReportStore
        from agents.subagent.models import STATUS_NO_DATA, SubagentResult

        class NoData:
            def run(self, task, cancel=None):
                return SubagentResult(STATUS_NO_DATA, "no-data")

        store = DeepReportStore(tmp_path)
        store.create(ReportMeta(rid="rc", universe="银行板块", user_request="",
                                template_id="deep_sector_finance", variant="finance"))
        svc = FetchService(executor=NoData(), store=store, rid="rc",
                           derive_fetcher=lambda name: self._rows(12))
        card = svc.fetch_capability("constituents", "银行板块",
                                    candidates=[{"code": "BK1283", "name": "银行", "kind": "board"}])
        assert card["status"] == "ok" and card["items"]
        assert card["charts"]

"""M4 统稿单测：确定性校验 + 规则审稿（不调 LLM）。"""
from __future__ import annotations

from agents.deep_report.models import Outline, OutlineSection, SectionResult
from agents.deep_report.qa import (
    card_metrics, check_gap_mentions, check_metric_consistency, run_qa,
)
from agents.deep_report.review import claims_of, review


def _cards():
    return {"valuation": {
        "ev_id": "ev_001", "slot": "valuation", "status": "ok", "asof": "2026-10-30",
        "items": [{"metric": "PB(LF)", "value": 0.68, "unit": "倍", "source": "mx"},
                  {"metric": "ROE(TTM)", "value": 8.6, "unit": "%", "source": "mx"}],
        "gaps": [],
    }}


def _outline():
    return Outline(title="T", template_id="deep_sector_finance", variant="finance",
                   sections=[OutlineSection(1, "summary", "投资摘要", [100, 200]),
                             OutlineSection(2, "valuation", "估值", [100, 200])])


class TestQA:
    def test_metric_consistent_passes(self):
        secs = [SectionResult(1, "summary", "投资摘要", "ok",
                              "板块 PB(LF) 0.68 倍，ROE(TTM) 8.6%（mx，2026-10-30）", 40)]
        conflicts = check_metric_consistency(secs, _cards())
        assert conflicts == []

    def test_metric_conflict_detected(self):
        secs = [SectionResult(1, "summary", "投资摘要", "ok",
                              "板块 PB(LF) 0.99 倍，明显更低", 30)]
        conflicts = check_metric_consistency(secs, _cards())
        assert conflicts and conflicts[0]["metric"] == "pb(lf)"
        assert conflicts[0]["expected"] == 0.68
        assert conflicts[0]["seen"][0]["seq"] == 1

    def test_gap_mentions_limited(self):
        many = "；".join(f"数据缺失：字段{i}" for i in range(11))
        secs = [SectionResult(1, "s", "t", "ok", many, 20)]
        assert not check_gap_mentions(secs)["passed"], "11 个不同缺口字段应判不合格"
        few = "数据缺失：A；数据缺失：A；数据缺失：B"
        assert check_gap_mentions([SectionResult(2, "s", "t", "ok", few, 20)])["passed"], "重复提及同一字段不叠加"

    def test_run_qa_shape(self):
        secs = [SectionResult(1, "summary", "投资摘要", "ok", "PB(LF) 0.99 倍", 30)]
        qa = run_qa(secs, _outline(), _cards())
        assert set(qa) == {"checks", "conflicts", "passed"} and not qa["passed"]

    def test_card_metrics_normalized(self):
        m = card_metrics(_cards())
        assert "pb(lf)" in m and m["roe(ttm)"][0]["value"] == 8.6

    def test_series_compares_against_latest_not_oldest(self):
        """多期序列升序时，首条是最旧值；不能拿它当「当前值」基准。"""
        cards = {"valuation": {"ev_id": "ev_s", "slot": "valuation", "status": "ok",
                               "asof": "2026-10-30", "items": [
                                   {"metric": "PB", "value": 0.50, "unit": "倍", "source": "mx",
                                    "period": "2026-01-01"},
                                   {"metric": "PB", "value": 0.68, "unit": "倍", "source": "mx",
                                    "period": "2026-10-30"}]}}
        ok = [SectionResult(1, "s", "t", "ok", "PB 0.68 倍", 10)]
        assert check_metric_consistency(ok, cards) == []
        bad = [SectionResult(1, "s", "t", "ok", "PB 0.50 倍", 10)]
        assert check_metric_consistency(bad, cards), "旧值不应被当作基准"


class TestTitleCheck:
    def test_title_supported(self):
        from agents.deep_report.qa import check_title_support
        secs = [SectionResult(1, "summary", "投资摘要", "ok",
                              "板块估值修复进入中段；追高需要等待资金确认与息差信号。", 30)]
        r = check_title_support("银行板块：估值修复中段，追高需待资金确认", secs)
        assert r["passed"], r["detail"]

    def test_title_unsupported(self):
        from agents.deep_report.qa import check_title_support
        secs = [SectionResult(1, "summary", "投资摘要", "ok", "本节只讨论分红与不良率。", 20)]
        r = check_title_support("银行板块：估值修复中段，追高需待资金确认", secs)
        assert not r["passed"]
        assert "估值修复中段" in r["detail"]

    def test_empty_title_passes(self):
        from agents.deep_report.qa import check_title_support
        assert check_title_support("", [])["passed"]


class TestReview:
    def test_claims_extracted(self):
        sec = SectionResult(1, "s", "t", "ok",
                            "## 标题\n- 结论一：板块估值不便宜\n- 结论二：资金面转弱\n| a | b |", 10)
        claims = claims_of(sec)
        assert claims and "结论一" in claims[0]

    def test_rule_rewrites_from_conflict(self):
        secs = [SectionResult(1, "summary", "投资摘要", "ok", "PB(LF) 0.99 倍", 30)]
        qa = run_qa(secs, _outline(), _cards())
        r = review(_outline(), secs, qa, llm=None)
        assert r["verdict"] == "revise" and r["rewrites"][0]["seq"] == 1
        assert "0.68" in r["rewrites"][0]["fix"]

    def test_ok_when_qa_passed(self):
        qa = {"passed": True, "checks": [], "conflicts": []}
        r = review(_outline(), [], qa, llm=None, semantic=False)
        assert r["verdict"] == "ok" and r["rewrites"] == []

    def test_semantic_path_parses_issues_and_thesis_fix(self):
        class FakeLLM:
            def invoke(self, messages, **kwargs):
                class R:
                    content = ('{"verdict":"revise","reason":"摘要与thesis矛盾",'
                               '"semantic_issues":["第1章说中性，thesis说看好"],'
                               '"thesis_fix":"中性：估值修复过半，等待回调",'
                               '"title_fix":"银行板块：修复过半，等待回调",'
                               '"rewrites":[{"seq":1,"fix":"结论改为中性","keep":"其余不变"}]}')
                return R()

        qa = {"passed": True, "checks": [], "conflicts": []}
        r = review(_outline(), [SectionResult(1, "summary", "投资摘要", "ok", "正文", 10)],
                   qa, llm=FakeLLM(), semantic=True)
        assert r["semantic_issues"] and r["thesis_fix"].startswith("中性")
        assert r["title_fix"].startswith("银行板块")
        assert r["rewrites"][0]["seq"] == 1

    def test_prompt_asks_for_title_fix(self):
        from agents.deep_report.review import _prompt
        p = _prompt(_outline(), [], {"passed": True, "checks": [], "conflicts": []}, True)
        assert "title_fix" in p and "语义一致性" in p


    def _board_cards(self):
        return {"valuation": {"ev_id": "ev_s", "slot": "valuation", "status": "ok",
                              "asof": "2026-10-30",
                              "items": [{"metric": "银行板块 PB 中位数", "value": 0.57,
                                         "unit": "倍", "source": "x"}], "gaps": []}}

    def test_metric_shorthand_matches(self):
        """「银行板块 PB 中位数」应能被「PB 中位数」命中（原来会漏报）。"""
        secs = [SectionResult(1, "s", "t", "ok", "PB 中位数 0.57 倍", 10)]
        assert check_metric_consistency(secs, self._board_cards()) == []

    def test_metric_date_in_parens_not_conflict(self):
        """括号里的日期（2026-03-31）不能被当成指标值。"""
        secs = [SectionResult(1, "s", "t", "ok",
                              "PB 中位数（2026-03-31 口径）为 0.57 倍", 10)]
        assert check_metric_consistency(secs, self._board_cards()) == []

    def test_metric_number_after_date_is_checked(self):
        """日期之后的真实数值仍要被抓到（0.72 ≠ 0.57）。"""
        secs = [SectionResult(1, "s", "t", "ok",
                              "PB 中位数（2026-03-31 口径）为 0.72 倍", 10)]
        conflicts = check_metric_consistency(secs, self._board_cards())
        assert conflicts and abs(conflicts[0]["seen"][0]["value"] - 0.72) < 1e-9

    def test_metric_without_number_skipped(self):
        secs = [SectionResult(1, "s", "t", "ok", "PB 中位数（口径见附录）", 10)]
        assert check_metric_consistency(secs, self._board_cards()) == []


    def _two_pb_cards(self):
        return {
            "snapshot": {"ev_id": "ev_pb", "slot": "snapshot", "status": "ok",
                         "asof": "2026-10-30",
                         "items": [{"metric": "PB", "value": 0.68, "unit": "倍", "source": "x"}],
                         "gaps": []},
            "valuation": {"ev_id": "ev_med", "slot": "valuation", "status": "ok",
                          "asof": "2026-10-30",
                          "items": [{"metric": "银行板块 PB 中位数", "value": 0.57,
                                     "unit": "倍", "source": "x"}], "gaps": []},
        }

    def test_cross_card_pb_vs_median(self):
        """PB=0.68 与 PB 中位数=0.57 并存时，正文写 0.57 不能被判成冲突（评审 P1）。"""
        secs = [SectionResult(1, "s", "t", "ok", "行业 PB 中位数 0.57 倍，估值低位。", 10)]
        assert check_metric_consistency(secs, self._two_pb_cards()) == []

    def test_cross_card_pb_plain_still_checked(self):
        secs = [SectionResult(1, "s", "t", "ok", "行业 PB 0.99 倍，明显偏高。", 10)]
        conflicts = check_metric_consistency(secs, self._two_pb_cards())
        assert conflicts and conflicts[0]["metric"] == "pb"
        assert abs(conflicts[0]["expected"] - 0.68) < 1e-9

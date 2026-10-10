"""快照差分告警测试（纯函数）。

核心契约：**只报变化，不报状态**。
「一直陈旧」「一直是 0 分」不该每天刷一条，只有「变了」才值得提醒——
否则告警列表会被噪音淹没，用户就不看了。
"""
import pytest

from thesis import alerts as A


def _snap(**over) -> dict:
    base = {
        "thesis_id": "t_env",
        "trade_date": "2026-09-17",
        "total": 4,
        "valid_count": 5,
        "verdict": "中性偏有利",
        "action": "可持有",
        "factors": [
            {"key": "premium", "name": "① 股息率溢价", "value": 2.59, "unit": "pp",
             "score": 0, "status": "中性", "asof": "2026-09-17", "stale": False},
            {"key": "cn10_y", "name": "② 10Y 国债", "value": 1.6862, "unit": "%",
             "score": 1, "status": "有利", "asof": "2026-09-17", "stale": False},
        ],
        "invalidation": [{"text": "10Y 国债升破 2.10%", "current": "1.6862%", "hit": False}],
    }
    base.update(over)
    return base


def _types(alerts):
    return [a["alert_type"] for a in alerts]


def _by_type(alerts, t):
    return [a for a in alerts if a["alert_type"] == t]


class TestNoNoise:
    def test_first_snapshot_produces_nothing(self):
        """首次快照没有前值可比，硬比就是噪音。"""
        assert A.diff_snapshots(None, _snap()) == []

    def test_identical_snapshots_produce_nothing(self):
        assert A.diff_snapshots(_snap(), _snap()) == []

    def test_persistently_stale_not_reported_again(self):
        stale = {"key": "cn10_y", "name": "② 10Y 国债", "value": 1.69, "unit": "%",
                 "score": 1, "status": "有利", "asof": "2026-08-01", "stale": True}
        prev = _snap(factors=[stale])
        cur = _snap(factors=[dict(stale, asof="2026-08-02")])
        assert _by_type(A.diff_snapshots(prev, cur), "factor_stale") == []


class TestInvalidationHit:
    def test_newly_hit_is_critical(self):
        prev = _snap()
        cur = _snap(invalidation=[{"text": "10Y 国债升破 2.10%",
                                   "current": "2.20%", "hit": True}])
        alerts = A.diff_snapshots(prev, cur)
        hit = _by_type(alerts, "invalidation_hit")
        assert len(hit) == 1
        assert hit[0]["severity"] == "critical"
        assert "10Y 国债升破 2.10%" in hit[0]["message"]
        assert "2.20%" in hit[0]["message"]

    def test_still_hit_is_not_reported(self):
        rule = {"text": "T", "current": "x", "hit": True}
        assert A.diff_snapshots(_snap(invalidation=[rule]),
                                _snap(invalidation=[rule])) == []

    def test_recovered_is_not_reported(self):
        """失效条件解除不告警（结论恢复不需要打扰，快照本身就说明了）。"""
        prev = _snap(invalidation=[{"text": "T", "current": "x", "hit": True}])
        cur = _snap(invalidation=[{"text": "T", "current": "y", "hit": False}])
        assert A.diff_snapshots(prev, cur) == []


class TestVerdictChange:
    def test_verdict_change_is_warn_with_both_sides(self):
        alerts = A.diff_snapshots(_snap(verdict="中性偏有利", total=4),
                                  _snap(verdict="中性", total=0, action="观望"))
        vc = _by_type(alerts, "verdict_change")
        assert len(vc) == 1
        assert vc[0]["severity"] == "warn"
        assert "中性偏有利" in vc[0]["message"] and "中性" in vc[0]["message"]
        assert vc[0]["detail"]["prev_total"] == 4
        assert vc[0]["detail"]["cur_total"] == 0


class TestFactorFlip:
    def test_score_drop_is_warn(self):
        prev = _snap(factors=[{"key": "premium", "name": "① 股息率溢价", "value": 2.9,
                               "unit": "pp", "score": 1, "status": "有利",
                               "asof": "2026-09-17", "stale": False}])
        cur = _snap(factors=[{"key": "premium", "name": "① 股息率溢价", "value": 2.5,
                              "unit": "pp", "score": 0, "status": "中性",
                              "asof": "2026-09-17", "stale": False}])
        alerts = _by_type(A.diff_snapshots(prev, cur), "factor_flip")
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "warn"
        assert "+1 → +0" in alerts[0]["message"]
        assert alerts[0]["detail"] == {"factor": "premium", "prev_score": 1, "cur_score": 0}

    def test_score_rise_is_info(self):
        prev = _snap(factors=[{"key": "premium", "name": "①", "value": 1.0, "unit": "pp",
                               "score": -1, "status": "偏不利", "asof": "d", "stale": False}])
        cur = _snap(factors=[{"key": "premium", "name": "①", "value": 3.2, "unit": "pp",
                              "score": 2, "status": "很有利", "asof": "d", "stale": False}])
        alerts = _by_type(A.diff_snapshots(prev, cur), "factor_flip")
        assert alerts[0]["severity"] == "info"

    def test_new_factor_key_ignored(self):
        """新增因子不告警（它是新增，不是变化）。"""
        prev = _snap(factors=[{"key": "premium", "name": "①", "value": 1, "unit": "pp",
                               "score": 0, "status": "中性", "asof": "d", "stale": False}])
        cur = _snap(factors=[
            {"key": "premium", "name": "①", "value": 1, "unit": "pp", "score": 0,
             "status": "中性", "asof": "d", "stale": False},
            {"key": "new", "name": "⑥", "value": 1, "unit": "%", "score": 1,
             "status": "有利", "asof": "d", "stale": False},
        ])
        assert A.diff_snapshots(prev, cur) == []

    def test_data_missing_and_restored(self):
        has = {"key": "cn10_y", "name": "② 10Y 国债", "value": 1.69, "unit": "%",
               "score": 1, "status": "有利", "asof": "d", "stale": False}
        missing = dict(has, value=None, score=None, status="数据缺失")

        gone = _by_type(A.diff_snapshots(_snap(factors=[has]),
                                         _snap(factors=[missing])), "factor_data_missing")
        assert len(gone) == 1 and gone[0]["severity"] == "warn"
        assert "退出综合评分" in gone[0]["message"]

        back = _by_type(A.diff_snapshots(_snap(factors=[missing]),
                                         _snap(factors=[has])), "factor_data_restored")
        assert len(back) == 1 and back[0]["severity"] == "info"


class TestStaleness:
    def test_newly_stale_is_warn(self):
        fresh = {"key": "cn10_y", "name": "② 10Y 国债", "value": 1.69, "unit": "%",
                 "score": 1, "status": "有利", "asof": "2026-09-17", "stale": False}
        stale = dict(fresh, asof="2026-08-01", stale=True)
        alerts = _by_type(A.diff_snapshots(_snap(factors=[fresh]),
                                           _snap(factors=[stale])), "factor_stale")
        assert len(alerts) == 1
        assert "2026-08-01" in alerts[0]["message"]


class TestCoverageDrop:
    def test_valid_count_drop_is_warn(self):
        alerts = _by_type(A.diff_snapshots(_snap(valid_count=5),
                                           _snap(valid_count=3)), "coverage_drop")
        assert len(alerts) == 1
        assert "5 降至 3" in alerts[0]["message"]

    def test_valid_count_rise_not_reported(self):
        assert _by_type(A.diff_snapshots(_snap(valid_count=3),
                                         _snap(valid_count=5)), "coverage_drop") == []


class TestSummaryHelpers:
    def test_summarize_counts_by_severity(self):
        alerts = [
            {"severity": "critical"}, {"severity": "warn"},
            {"severity": "warn"}, {"severity": "info"},
        ]
        s = A.summarize(alerts)
        assert s == {"info": 1, "warn": 2, "critical": 1, "total": 4}

    def test_summarize_empty(self):
        assert A.summarize([])["total"] == 0

    def test_top_severity(self):
        assert A.top_severity([]) is None
        assert A.top_severity([{"severity": "info"}, {"severity": "critical"}]) == "critical"
        assert A.top_severity([{"severity": "info"}, {"severity": "warn"}]) == "warn"

    def test_all_alerts_carry_thesis_and_date(self):
        cur = _snap(verdict="中性")
        for a in A.diff_snapshots(_snap(), cur):
            assert a["thesis_id"] == "t_env"
            assert a["trade_date"] == "2026-09-17"
            assert a["message"]

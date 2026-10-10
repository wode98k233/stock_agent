"""台账定义加载与校验测试。

校验器的存在意义：阈值区间没兜住 / 指标名写错 会让因子**静默**变成「数据缺失」，
比报错更难查。这里把这些坑逐个锁成用例。
"""
import json

import pytest

from thesis import defs as thesis_defs


def _base_def() -> dict:
    return {
        "id": "t",
        "name": "测试台账",
        "version": "1.0",
        "subject": {"index_code": "H30269", "benchmark_code": "000300"},
        "thresholds": {"a": [[-99, 99, 1]]},
        "verdicts": [{"min_total": -99, "verdict": "中性", "action": "观望"}],
        "signals": {
            "buy": [{"metric": "premium", "op": ">", "value": 1, "label": "溢价>1"}],
            "warn": [{"metric": "cn10_y", "op": "<", "value": 2, "label": "国债<2"}],
        },
        "invalidation": {
            "rules": [{"metric": "premium", "op": "<", "value": 1, "text": "补偿不足"}]
        },
    }


def _load_from(tmp_path, monkeypatch, payload: dict):
    (tmp_path / "t.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(thesis_defs, "get_defs_dir", lambda: tmp_path)
    thesis_defs._cache.clear()
    return thesis_defs.load_def_checked("t")


class TestRealDefinition:
    """真实定义必须自洽——不然线上就是「数据缺失」而没人知道为什么。"""

    def test_dividend_env_loaded_and_valid(self):
        defn, problems = thesis_defs.load_def_checked("dividend_env")
        assert defn is not None, problems
        assert problems == [], problems

    def test_dividend_env_listed(self):
        assert "dividend_env" in thesis_defs.list_defs()

    def test_unknown_id_returns_problem(self):
        defn, problems = thesis_defs.load_def_checked("no_such_thesis")
        assert defn is None
        assert problems

    def test_cached_by_mtime_returns_same_object(self):
        thesis_defs._cache.clear()
        first = thesis_defs.load_def("dividend_env")
        second = thesis_defs.load_def("dividend_env")
        assert first is second  # 同一 mtime 不重复解析

    def test_threshold_tables_cover_full_range(self):
        """真实定义的每张阈值表都必须兜住两端，否则会出现「有数据却算不出分」。"""
        defn = thesis_defs.load_def("dividend_env")
        for name, table in (defn.get("thresholds") or {}).items():
            lo_min = min(r[0] for r in table)
            hi_max = max(r[1] for r in table)
            assert lo_min <= thesis_defs._NEG_INF, name
            assert hi_max >= thesis_defs._POS_INF, name


class TestValidation:
    def test_valid_base_passes(self, tmp_path, monkeypatch):
        defn, problems = _load_from(tmp_path, monkeypatch, _base_def())
        assert defn is not None
        assert problems == [], problems

    def test_threshold_not_covering_low_end(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["thresholds"]["a"] = [[0, 99, 1]]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("未覆盖低端" in p for p in problems), problems

    def test_threshold_not_covering_high_end(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["thresholds"]["a"] = [[-99, 10, 1]]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("未覆盖高端" in p for p in problems), problems

    def test_threshold_bad_row_format(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["thresholds"]["a"] = [[-99, 99]]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("格式错误" in p for p in problems), problems

    def test_threshold_empty_interval(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["thresholds"]["a"] = [[-99, 99, 1], [5, 5, 0]]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("空区间" in p for p in problems), problems

    def test_threshold_score_out_of_range(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["thresholds"]["a"] = [[-99, 99, 5]]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("超出" in p for p in problems), problems

    def test_unknown_metric_detected(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["signals"]["buy"][0]["metric"] = "typo_metric"
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("未知指标" in p for p in problems), problems

    def test_bad_operator_detected(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["signals"]["buy"][0]["op"] = "="
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("非法运算符" in p for p in problems), problems

    def test_missing_subject_code_detected(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["subject"] = {"index_code": "H30269"}
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("benchmark_code" in p for p in problems), problems

    def test_verdicts_without_fallback_detected(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["verdicts"] = [{"min_total": 5, "verdict": "偏有利", "action": "买"}]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("未兜底低分" in p for p in problems), problems

    def test_manual_item_needs_stale_days_and_updated_at(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["manual"] = {"insurance_oci": {"gap_yi": 1000}}
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("stale_days" in p for p in problems), problems
        assert any("updated_at" in p for p in problems), problems

    def test_invalidation_requires_text(self, tmp_path, monkeypatch):
        payload = _base_def()
        payload["invalidation"]["rules"] = [{"metric": "premium", "op": "<", "value": 1}]
        _, problems = _load_from(tmp_path, monkeypatch, payload)
        assert any("缺 text" in p for p in problems), problems

    def test_broken_json_returns_none(self, tmp_path, monkeypatch):
        (tmp_path / "t.json").write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(thesis_defs, "get_defs_dir", lambda: tmp_path)
        thesis_defs._cache.clear()
        defn, problems = thesis_defs.load_def_checked("t")
        assert defn is None
        assert any("无法解析" in p for p in problems), problems


class TestScoreByTable:
    def test_lower_bound_inclusive_upper_exclusive(self):
        table = [[3.0, 99, 2], [2.0, 3.0, 0], [-99, 2.0, -2]]
        assert thesis_defs.score_by_table(3.0, table) == 2
        assert thesis_defs.score_by_table(2.9999, table) == 0
        assert thesis_defs.score_by_table(2.0, table) == 0
        assert thesis_defs.score_by_table(1.9999, table) == -2

    def test_order_independent(self):
        asc = [[-99, 2.0, -2], [2.0, 3.0, 0], [3.0, 99, 2]]
        desc = [[3.0, 99, 2], [2.0, 3.0, 0], [-99, 2.0, -2]]
        for v in (-5, 2.0, 2.5, 3.0, 50):
            assert thesis_defs.score_by_table(v, asc) == thesis_defs.score_by_table(v, desc)

    def test_none_value_returns_none(self):
        assert thesis_defs.score_by_table(None, [[-99, 99, 0]]) is None

    def test_out_of_range_returns_none(self):
        """区间未兜住时返回 None——这是要靠 validate_def 在上游拦住的形态。"""
        assert thesis_defs.score_by_table(200, [[-99, 99, 0]]) is None

"""拥挤度标的池与阈值校验测试。

存在理由：阈值表是**绝对刻度**判分（不是池内分位），一旦某一档没兜住两端或留了缺口，
落在缺口里的值会被 `score_by_table` 静默判成「数据缺失」——页面上只看到一堆「待补数」，
查半天才发现是配置写错了。所以校验必须有测试盯着。
"""
import json

import pytest

from crowding.pool import (
    DEFAULT_POOL,
    _deep_merge,
    _validate_thresholds,
    level_of,
    load_pool,
    validate_pool,
)


class TestDefaultPool:
    def test_default_pool_is_clean(self):
        assert validate_pool(DEFAULT_POOL) == []

    def test_weights_sum_to_one(self):
        assert abs(sum(DEFAULT_POOL["weights"].values()) - 1.0) < 1e-9

    def test_every_threshold_covers_both_ends(self):
        for key, table in DEFAULT_POOL["thresholds"].items():
            rows = sorted(table, key=lambda r: r[0])
            assert rows[0][0] <= -99, f"{key} 未兜住下界"
            assert rows[-1][1] >= 99, f"{key} 未兜住上界"

    def test_levels_are_ascending_and_cover_100(self):
        bounds = [b for b, _lbl in DEFAULT_POOL["levels"]]
        assert bounds == sorted(bounds)
        assert bounds[-1] >= 100


class TestThresholdValidation:
    def test_contiguous_table_is_clean(self):
        assert _validate_thresholds("x", [[-99, 0, 10], [0, 99, 50]]) == []

    def test_missing_lower_bound_reported(self):
        problems = _validate_thresholds("x", [[0, 10, 50], [10, 99, 80]])
        assert any("未兜住下界" in p for p in problems)

    def test_missing_upper_bound_reported(self):
        problems = _validate_thresholds("x", [[-99, 0, 10], [0, 98, 50]])
        assert any("未兜住上界" in p for p in problems)

    def test_empty_interval_reported(self):
        """老坑：上一档上限 99 而下一档从 99 起 → 该区间永远匹配不到。"""
        problems = _validate_thresholds("x", [[-99, 0, 10], [0, 0, 50], [0, 99, 80]])
        assert any("区间为空" in p for p in problems)

    def test_gap_reported(self):
        problems = _validate_thresholds("x", [[-99, 0, 10], [5, 99, 50]])
        assert any("缺口" in p for p in problems)

    def test_non_triple_row_reported(self):
        problems = _validate_thresholds("x", [[-99, 0], [0, 99, 50]])
        assert any("[下限,上限,得分]" in p for p in problems)

    def test_empty_table_reported(self):
        assert _validate_thresholds("x", []) == ["阈值表 x 为空"]

    def test_none_table_reported(self):
        assert _validate_thresholds("x", None) == ["阈值表 x 为空"]


class TestValidatePool:
    def test_missing_threshold_key(self):
        pool = dict(DEFAULT_POOL)
        pool["thresholds"] = {k: v for k, v in DEFAULT_POOL["thresholds"].items() if k != "mom"}
        problems = validate_pool(pool)
        assert any("thresholds.mom" in p for p in problems)

    def test_weight_sum_deviation_reported(self):
        pool = {**DEFAULT_POOL,
                "weights": {"ext": 0.5, "mom": 0.5, "vol": 0.5, "chase": 0.5}}
        assert any("偏离 1" in p for p in validate_pool(pool))

    def test_missing_weight_key(self):
        pool = {**DEFAULT_POOL, "weights": {"ext": 0.5, "mom": 0.5}}
        problems = validate_pool(pool)
        assert any("缺少权重" in p and "vol" in p for p in problems)

    def test_levels_not_ascending(self):
        pool = {**DEFAULT_POOL, "levels": [[40, "舒适"], [30, "中性"], [101, "过热"]]}
        assert any("未升序" in p for p in validate_pool(pool))

    def test_levels_must_cover_100(self):
        pool = {**DEFAULT_POOL,
                "levels": [[40, "舒适"], [60, "中性"], [75, "偏挤"], [85, "拥挤"]]}
        assert any("末档未覆盖到 100" in p for p in validate_pool(pool))

    def test_levels_must_be_non_empty(self):
        pool = {**DEFAULT_POOL, "levels": []}
        assert any("缺少 levels" in p for p in validate_pool(pool))

    def test_bad_params_reported(self):
        pool = {**DEFAULT_POOL, "params": {**DEFAULT_POOL["params"], "ma_days": 0}}
        assert any("params.ma_days" in p for p in validate_pool(pool))

    def test_non_positive_stale_days_reported(self):
        pool = {**DEFAULT_POOL, "params": {**DEFAULT_POOL["params"], "stale_days": -1}}
        assert any("params.stale_days" in p for p in validate_pool(pool))


class TestDeepMerge:
    def test_override_wins_and_defaults_survive(self):
        merged = _deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
        assert merged == {"a": {"b": 9, "c": 2}}

    def test_lists_are_replaced_wholesale(self):
        merged = _deep_merge({"a": {"c": [1, 2]}}, {"a": {"c": [9]}})
        assert merged["a"]["c"] == [9]

    def test_base_is_not_mutated(self):
        base = {"a": {"b": 1}}
        _deep_merge(base, {"a": {"b": 2}})
        assert base == {"a": {"b": 1}}

    def test_empty_override_returns_copy(self):
        base = {"a": {"b": 1}}
        out = _deep_merge(base, None)
        assert out == base and out is not base


class TestLoadPool:
    def test_missing_file_falls_back_with_problem(self, tmp_path):
        pool, problems = load_pool(str(tmp_path / "nope.json"))
        assert pool["params"]["ma_days"] == DEFAULT_POOL["params"]["ma_days"]
        assert any("不存在" in p for p in problems)

    def test_broken_json_falls_back(self, tmp_path):
        f = tmp_path / "pool.json"
        f.write_text("{ not json", encoding="utf-8")
        pool, problems = load_pool(str(f))
        assert pool["weights"] == DEFAULT_POOL["weights"]
        assert any("解析失败" in p for p in problems)

    def test_non_object_root_falls_back(self, tmp_path):
        f = tmp_path / "pool.json"
        f.write_text("[1, 2]", encoding="utf-8")
        pool, problems = load_pool(str(f))
        assert any("根节点不是对象" in p for p in problems)
        assert pool["name"] == DEFAULT_POOL["name"]

    def test_partial_override_deep_merges_with_defaults(self, tmp_path):
        f = tmp_path / "pool.json"
        f.write_text(json.dumps({"params": {"top_n": 5}}, ensure_ascii=False), encoding="utf-8")
        pool, problems = load_pool(str(f))
        assert pool["params"]["top_n"] == 5
        assert pool["params"]["ma_days"] == DEFAULT_POOL["params"]["ma_days"]
        assert pool["boards"]["enabled"] is DEFAULT_POOL["boards"]["enabled"]
        assert problems == []

    def test_problems_surface_from_file_content(self, tmp_path):
        """文件里的阈值写坏了，load_pool 必须报出来而不是默默跑。"""
        f = tmp_path / "pool.json"
        f.write_text(json.dumps({"thresholds": {"ext": [[0, 10, 50]]}}), encoding="utf-8")
        _pool, problems = load_pool(str(f))
        assert any("未兜住下界" in p for p in problems)

    def test_shipped_pool_json_is_valid(self):
        """data/crowding/pool.json 是随包发布的默认定义，必须校验干净。

        只校验「合法」不校验「等于内置默认」—— 用户按设计就是可以手改这个文件调参的。
        """
        pool, problems = load_pool()
        assert problems == []
        assert pool["levels"] and pool["thresholds"]
        assert set(pool["weights"]) == {"ext", "mom", "vol", "chase"}


class TestLevelOf:
    @pytest.mark.parametrize("score,expect", [
        (0, "舒适"), (39.9, "舒适"),
        (40, "中性"), (59.9, "中性"),
        (60, "偏挤"), (74.9, "偏挤"),
        (75, "拥挤"), (84.9, "拥挤"),
        (85, "过热"), (100, "过热"), (200, "过热"),
    ])
    def test_boundaries(self, score, expect):
        assert level_of(score, DEFAULT_POOL["levels"]) == expect

    def test_none_score_returns_none(self):
        assert level_of(None, DEFAULT_POOL["levels"]) is None

    def test_zero_score_is_a_level_not_missing(self):
        """0 分是「读数很低」，不能被 falsy 判断吃掉当成缺数据。"""
        assert level_of(0, DEFAULT_POOL["levels"]) == "舒适"

    def test_empty_levels_returns_none(self):
        assert level_of(50, []) is None

    def test_hot_score_lands_in_hot_levels(self):
        """hot_score=75 与 levels 必须自洽：≥hot_score 的标的不能落在「偏挤」以下。"""
        hot = DEFAULT_POOL["params"]["hot_score"]
        assert level_of(hot, DEFAULT_POOL["levels"]) in ("拥挤", "过热")

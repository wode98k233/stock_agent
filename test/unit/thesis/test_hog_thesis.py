"""猪周期台账（hog_regime）：6 因子 + pos_keys 三角形 B 轴，完全离线。

为什么单独一个文件：
1. **B 轴方向与 cu_regime 相反（本文件最核心的守卫）**。cu 的 B 轴「正分 = 便宜」，
   hog 的 B 轴「**正分 = 价格高位**」——两张台账各自沿用原始脚本口径，不是笔误。
   后果是同一句「B = −3」，在 cu 里读作「价格很便宜」，在 hog 里读作「价格很低」，
   但档位名从「高位」变成「低位」。若有人把 cu 的阈值符号照抄过来，④⑤⑥ 会全部反向、
   「左侧布局期」会变成「景气兑现期」，正好把结论反过来。故这里逐条钉住符号。
2. **pos_keys 是本台账逼出来的新能力**：B 轴 = ④+⑤+⑥ 三因子求和（原始脚本的
   `B = sum(rank+basis+ma12)`）。只取 ④ 会把 B 轴满分从 ±5 缩到 ±2、档位整体失真。
   这里既测引擎语义（见 test_cu_thesis.py 的 TestStageMatrix），也测「真实定义确实声明了 3 项」。
3. **手工项的 derive 派生**：能繁存栏 3780 万头本身无好坏，被打分的是「较高点 4080
   去化 7.35%」。这是 `_factor_manual` 新增的 derive 能力，也最容易悄悄退化回
   「对 3780 打分」（那样永远落在最高档，因为 3780 < 所有阈值）。
4. **口径陷阱：index ≠ price**。现货指数源自带 index/price/weight/ma12 四列，
   ④ 用 price、⑥ 用 index/ma12。两者数值差一个量级（首跑 index=72.71、price=10.56），
   混用会得到看似合理的错数。
"""
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from thesis import defs
from thesis.engine import ThesisEngine

TODAY = date(2026, 9, 25)
ROOT = Path(__file__).resolve().parents[3]
HOG_DEF = ROOT / "data" / "thesis" / "hog_regime.json"


def _daily(values, end=None):
    """按自然日倒推生成序列（升序）。猪台账多数序列是周频，但夹具用日频不影响逻辑。"""
    end = end or date(2026, 9, 24)
    n = len(values)
    return [{"report_date": (end - timedelta(days=n - 1 - i)).isoformat(), "value": float(v)}
            for i, v in enumerate(values)]


def _defn(factors, data, thresholds, stages=None, params=None, verdicts=None, manual=None):
    d = {
        "id": "t_hog", "name": "猪测试", "version": "1.0",
        "subject": {"index_code": "HOG_SPOT_IDX", "benchmark_code": "HOG_FUT_MAIN"},
        # data 必须声明：漏了 _prepare 不往 ctx 放序列 → 所有因子判「数据缺失」
        "data": data,
        "factors": factors,
        "params": params or {},
        "thresholds": thresholds,
        "verdicts": verdicts or [{"min_total": -99, "verdict": "x", "action": "y"}],
    }
    if stages is not None:
        d["stages"] = stages
    if manual is not None:
        d["manual"] = manual
    return d


class StubEngine(ThesisEngine):
    def __init__(self, series=None):
        super().__init__()
        self._series = series or {}

    def today(self):
        return TODAY

    def macro_stale_threshold(self, code):
        return 10

    def macro_series(self, code, periods=60):
        return list(self._series.get(str(code), []))


def _run(engine, defn):
    return engine.compute(defn["id"], defn=defn)


# ---------------------------------------------------------------------------
# 定义本身的守卫
# ---------------------------------------------------------------------------

class TestHogDefinition:
    @pytest.fixture(scope="class")
    def defn(self):
        return json.loads(HOG_DEF.read_text(encoding="utf-8"))

    def test_loads_and_validates(self):
        d, problems = defs.load_def_checked("hog_regime")
        assert d is not None
        assert problems == [], problems

    def test_listed(self):
        assert "hog_regime" in defs.list_defs()

    def test_every_compute_is_registered(self, defn):
        for f in defn["factors"]:
            assert f["compute"] in ThesisEngine.FACTOR_COMPUTERS, f["compute"]

    def test_every_threshold_key_exists(self, defn):
        for f in defn["factors"]:
            if f.get("thresholds_key"):
                assert f["thresholds_key"] in defn["thresholds"], f["key"]

    def test_scores_never_exceed_the_engine_cap(self, defn):
        """±2 是引擎硬上限；猪台账原本就是 ±2 制，无需重标定。"""
        for name, table in defn["thresholds"].items():
            for lo, hi, sc in table:
                assert defs.SCORE_MIN <= sc <= defs.SCORE_MAX, (name, sc)

    def test_threshold_tables_cover_the_whole_range(self, defn):
        for name, table in defn["thresholds"].items():
            assert min(r[0] for r in table) <= -99, name
            assert max(r[1] for r in table) >= 99, name

    def test_drive_full_matches_the_actual_achievable_max(self, defn):
        """A 轴 3 因子现实满分 = 2 + 2 + 1 = 5（③ 出栏均重刻意只有 ±1）。"""
        st = defn["stages"]
        by_key = {f["key"]: f for f in defn["factors"]}
        real_max = 0
        for k in st["drive_keys"]:
            table = defn["thresholds"][by_key[k]["thresholds_key"]]
            real_max += max(r[2] for r in table)
        assert st["drive_full"] == real_max == 5

    def test_weight_factor_is_deliberately_only_pm1(self, defn):
        """③ 出栏均重只有 ±1（行为信号的辅助确认，非主导变量）。

        若有人「顺手统一成 ±2」，A 轴满分从 5 变 6，drive_full 与 verdicts 档位漂移。
        """
        f = [x for x in defn["factors"] if x["key"] == "weight"][0]
        scores = {r[2] for r in defn["thresholds"][f["thresholds_key"]]}
        assert scores == {-1, 0, 1}

    def test_basis_factor_is_deliberately_only_pm1(self, defn):
        """⑤ 期现价差也只有 ±1（期货样本仅约 367 日，置信度 low）。"""
        f = [x for x in defn["factors"] if x["key"] == "basis"][0]
        scores = {r[2] for r in defn["thresholds"][f["thresholds_key"]]}
        assert scores == {-1, 0, 1}

    def test_pos_full_matches_the_actual_achievable_max(self, defn):
        """B 轴 3 因子现实满分 = 2 + 1 + 2 = 5（⑤ 期现价差只有 ±1）。"""
        st = defn["stages"]
        by_key = {f["key"]: f for f in defn["factors"]}
        real_max = 0
        for k in st["pos_keys"]:
            table = defn["thresholds"][by_key[k]["thresholds_key"]]
            real_max += max(r[2] for r in table)
        assert st["pos_full"] == real_max == 5

    def test_position_axis_is_a_three_factor_sum(self, defn):
        """⭐ B 轴必须是 ④+⑤+⑥ 三项求和，不能退化成单因子。"""
        st = defn["stages"]
        assert st["pos_keys"] == ["rank", "basis", "ma12"]
        assert st["pos_key"] == "rank"     # 兼容字段仍在，但 pos_keys 优先

    def test_position_keys_are_not_in_the_drive_axis(self, defn):
        st = defn["stages"]
        assert not (set(st["pos_keys"]) & set(st["drive_keys"]))

    def test_every_factor_belongs_to_exactly_one_axis(self, defn):
        st = defn["stages"]
        mine = set(st["drive_keys"]) | set(st["pos_keys"])
        assert mine == {f["key"] for f in defn["factors"]}

    def test_stage_cells_cover_all_nine_combinations(self, defn):
        st = defn["stages"]
        need = {(d["name"], p["name"])
                for d in st["drive_bands"] for p in st["pos_bands"]}
        got = {(c["drive"], c["pos"]) for c in st["cells"]}
        assert got == need, need - got
        names = [c["stage"] for c in st["cells"]]
        assert len(names) == len(set(names)) == 9

    def test_left_side_stage_sits_at_the_lower_left(self, defn):
        """⭐ 「左侧布局期」必须落在 出清充分(A高) × 价格低位(B低)。

        这是 B 轴极性（正分 = 价格高位）的**直接后果**：A 高 B 低 → 左下角。
        若极性被改成 cu 口径，这个 cell 会跑到右上角，报告的核心结论就反了。
        """
        st = defn["stages"]
        cell = [c for c in st["cells"] if c["stage"] == "左侧布局期"][0]
        assert cell["drive"] == "出清充分"
        assert cell["pos"] == "价格低位"
        # 档位定义：出清充分 min=2（高），价格低位是最后一档（min 最小）
        pos_bands = st["pos_bands"]
        assert [b["name"] for b in pos_bands][-1] == "价格低位"
        assert pos_bands[-1]["min"] <= -99

    def test_verdicts_ladder_is_strictly_descending_and_bottomed(self, defn):
        mins = [v["min_total"] for v in defn["verdicts"]]
        assert mins == sorted(mins, reverse=True)
        assert mins[-1] <= -99
        # 两轴现实满分 5+5=10
        assert mins[0] <= 10

    def test_position_axis_positive_means_price_is_high(self, defn):
        """⭐ B 轴方向约定：hog 的正分 = 价格高位（与 cu 相反，且**不是笔误**）。

        证据：④ 猪价分位是「低分位 → 负分、高分位 → 正分」；
        ⑤ 期现价差是「升水 → 正分」；⑥ 高于均线 → 正分。
        三条一致朝「价格越高 B 越高」。
        """
        assert defn["thresholds"]["price_rank_pct"][0][2] == -2   # 低分位 → −2
        assert defn["thresholds"]["price_rank_pct"][-1][2] == 2   # 高分位 → +2
        assert defn["thresholds"]["basis_pct"][-1][2] == 1        # 升水 → +
        assert defn["thresholds"]["vs_ma12_pct"][-1][2] == 2      # 高于均线 → +2

    def test_drive_axis_is_contrarian_on_loss_depth(self, defn):
        """⭐ A 轴是左侧逻辑：**亏损越深越有利**。

        ① 猪粮比越低越有利（低值 +2）；③ 出栏均重下降有利（负变动 +1）。
        与直觉相反但与周期机制一致——照抄「高猪粮比 = 行业好 = 有利」会整轴反号。
        """
        assert defn["thresholds"]["hog_corn_ratio"][0][2] == 2    # 猪粮比低 → +2
        assert defn["thresholds"]["hog_corn_ratio"][-1][2] == -2  # 猪粮比高 → −2
        assert defn["thresholds"]["weight_chg_pct"][0][2] == 1    # 均重下降 → +1
        assert defn["thresholds"]["weight_chg_pct"][-1][2] == -1  # 均重上升 → −1

    def test_sow_manual_declares_derive_and_metrics(self, defn):
        """② 能繁必须声明 derive（去化幅度）与 metrics（派生指标名），否则打错对象。"""
        f = [x for x in defn["factors"] if x["key"] == "sow"][0]
        assert f["compute"] == "manual"
        assert f.get("derive", {}).get("op") == "drop_pct"
        assert "sow_dev_pct" in (f.get("metrics") or [])
        assert f["thresholds_key"] == "sow_dev_pct"

    def test_sow_manual_block_has_peak_and_stale(self, defn):
        m = defn["manual"]["sow"]
        assert m["stale_days"] and m["updated_at"]
        assert m["peak"] > m["value"]        # 高点必须高于当前存栏，否则去化为负
        assert m["target"] < m["peak"]

    def test_ma12_uses_index_not_price(self, defn):
        """⑥ 必须用 index/ma12 配对，④ 用 price——两列不可互换（口径不同）。"""
        ma12 = [x for x in defn["factors"] if x["key"] == "ma12"][0]
        rank = [x for x in defn["factors"] if x["key"] == "rank"][0]
        assert ma12["a"] == "spot_index" and ma12["b"] == "spot_ma12"
        assert rank["series"] == "spot_price"
        assert ma12["a"] != rank["series"]


# ---------------------------------------------------------------------------
# manual + derive —— 能繁母猪去化幅度
# ---------------------------------------------------------------------------

_SOW_MANUAL = {
    "sow": {
        "label": "能繁母猪存栏", "value": 3780, "unit": "万头",
        "updated_at": "2026-06-30", "stale_days": 120,
        "peak": 4080, "peak_date": "2024-11-30", "target": 3750,
    }
}
_SOW_FACTOR = [{
    "key": "sow", "compute": "manual", "name": "② 能繁母猪去化",
    "manual": "sow", "value_field": "value",
    "metric": "sow_level", "metrics": ["sow_dev_pct"],
    "derive": {"op": "drop_pct", "of": "value", "from": "peak"},
    "stale_metric": "sow_stale", "unit": "万头",
    "thresholds_key": "t", "source": "测试",
    "detail_template": "存栏 {value} 万头（高点 {peak}）",
}]
_SOW_TH = {"t": [[-9999, 3, -1], [3, 5, 0], [5, 7, 1], [7, 9999, 2]]}


class TestManualDerive:
    def _defn(self, manual=None):
        return _defn(_SOW_FACTOR, {}, _SOW_TH, manual=manual or _SOW_MANUAL)

    def test_scores_the_derived_deviation_not_the_level(self):
        """读数 3780 本身无好坏；被打分的是「较 4080 去化了 7.35%」→ 落在 [7,∞) → +2。

        若 derive 退化，实现对 3780 打分——3780 远超所有阈值上限，
        会落到最后一档 [7, 9999) 也恰好是 +2……故**必须同时看 value 与 metric**，
        这里用一个能让两种口径分档不同的目标（target 档位）来区分：
        下面 test_derive_changes_the_band 用 5.5% 去化来把差异暴露出来。
        """
        r = _run(StubEngine({}), self._defn())
        f = r["factors"][0]
        assert f["value"] == pytest.approx(7.35, abs=0.01)
        assert f["score"] == 2
        # 原始读数仍以 metric 落库并展示在 detail 里
        assert r["metric_values"]["sow_level"] == 3780
        assert r["metric_values"]["sow_dev_pct"] == pytest.approx(7.35, abs=0.01)
        assert "3780" in f["detail"]

    def test_derive_changes_the_band(self):
        """去化 4.0%（较 4080 的 3916.8）→ 落在 [3,5) → 0 分。

        若实现对 3916.8 打分，它会落到最后一档 → +2，与正确的 0 分明显不同，
        这条断言就是 derive 有没有生效的判别器。
        """
        manual = {"sow": dict(_SOW_MANUAL["sow"], value=3916.8)}
        r = _run(StubEngine({}), self._defn(manual))
        f = r["factors"][0]
        assert f["value"] == pytest.approx(4.0, abs=0.05)
        assert f["score"] == 0

    def test_high_deviation_band(self):
        manual = {"sow": dict(_SOW_MANUAL["sow"], value=3876.0)}   # 去化 5.0%
        f = _run(StubEngine({}), self._defn(manual))["factors"][0]
        assert f["value"] == pytest.approx(5.0, abs=0.05)
        assert f["score"] == 1

    def test_stale_manual_warns_and_sets_stale_metric(self):
        """updated_at 距今超 stale_days → 告警 + stale_metric=1（信号可据此命中）。"""
        manual = {"sow": dict(_SOW_MANUAL["sow"], updated_at="2026-01-01")}
        w = []
        d = self._defn(manual)
        factor, updates = ThesisEngine._factor_manual(
            ThesisEngine(), d["factors"][0],
            {"series": {}, "frames": {}, "stale_days": {}}, d, {}, d["thresholds"], w)
        assert factor["stale"] is True
        assert updates["sow_stale"] == 1
        assert any("已超 120 天" in x for x in w)

    def test_fresh_manual_is_not_stale(self):
        d = self._defn()
        factor, updates = ThesisEngine._factor_manual(
            ThesisEngine(), d["factors"][0],
            {"series": {}, "frames": {}, "stale_days": {}}, d, {}, d["thresholds"], [])
        assert factor["stale"] is False
        assert updates["sow_stale"] == 0

    def test_missing_manual_value_is_missing_not_neutral(self):
        d = _defn(_SOW_FACTOR, {}, _SOW_TH, manual={"sow": {}})
        w = []
        factor, updates = ThesisEngine._factor_manual(
            ThesisEngine(), d["factors"][0],
            {"series": {}, "frames": {}, "stale_days": {}}, d, {}, d["thresholds"], w)
        assert factor["score"] is None and factor["value"] is None
        assert updates["sow_stale"] == 1
        assert any("无可用数值" in x for x in w)

    def test_derive_falls_back_to_raw_when_peak_missing(self):
        """peak 缺失时 derive 算不出 → 回退到原始 value（不抛错、不连累主流程）。"""
        d = _defn(_SOW_FACTOR, {}, _SOW_TH,
                  manual={"sow": {"value": 3780, "updated_at": "2026-09-20"}})
        factor, updates = ThesisEngine._factor_manual(
            ThesisEngine(), d["factors"][0],
            {"series": {}, "frames": {}, "stale_days": {}}, d, {}, d["thresholds"], [])
        assert factor["value"] == 3780
        assert updates["sow_dev_pct"] is None


# ---------------------------------------------------------------------------
# ratio_level（猪粮比）—— 已在 cu 文件测过通用语义，这里测本台账的方向
# ---------------------------------------------------------------------------

_RATIO_FACTOR = [{
    "key": "ratio", "compute": "ratio_level", "name": "① 猪粮比",
    "a": "hog", "b": "corn", "metric": "hog_corn_ratio",
    "unit": "", "label_a": "生猪", "label_b": "玉米",
    "thresholds_key": "t", "source": "测试",
}]
_RATIO_TH = {"t": [[-9999, 5, 2], [5, 6, 1], [6, 7, 0], [7, 8, -1], [8, 9999, -2]]}


class TestHogCornRatio:
    def _defn(self):
        return _defn(_RATIO_FACTOR,
                     {"hog": {"kind": "macro", "code": "H"},
                      "corn": {"kind": "macro", "code": "C"}},
                     _RATIO_TH)

    def test_deep_loss_ratio_scores_up(self):
        """猪粮比 4.5（<5:1 一级预警）→ +2：深度亏损 = 去化动力最强。"""
        f = _run(StubEngine({"H": _daily([9.0] * 25),
                             "C": _daily([2.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(4.5, abs=1e-9)
        assert f["score"] == 2

    def test_profit_ratio_scores_down(self):
        """猪粮比 8.5（高盈利）→ −2：行业回暖会重启补栏、推迟反转。"""
        f = _run(StubEngine({"H": _daily([17.0] * 25),
                             "C": _daily([2.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(8.5, abs=1e-9)
        assert f["score"] == -2


# ---------------------------------------------------------------------------
# vs_ma_gap（现货 vs 12 月均线）—— hog 的 ⑥ 与 cu 的 ⑥ 符号相反
# ---------------------------------------------------------------------------

_MA12_FACTOR = [{
    "key": "ma12", "compute": "vs_ma_gap", "name": "⑥ 现货 vs 12 月均线",
    "a": "spot_index", "b": "spot_ma12", "metric": "vs_ma12_pct",
    "raw_metric": "ma12_level", "unit": "%", "label": "生猪现货指数",
    "thresholds_key": "t", "source": "测试",
}]
_MA12_TH = {"t": [[-9999, -5, -2], [-5, 0, -1], [0, 5, 0], [5, 15, 1], [15, 9999, 2]]}


class TestHogMa12:
    def _defn(self):
        return _defn(_MA12_FACTOR,
                     {"spot_index": {"kind": "macro", "code": "IDX"},
                      "spot_ma12": {"kind": "macro", "code": "MA12"}},
                     _MA12_TH)

    def test_above_long_ma_scores_up(self):
        """⭐ 与 cu 的 ⑥ 相反：hog 高于长期均线 = 正分（B 轴正分 = 价格高位）。"""
        f = _run(StubEngine({"IDX": _daily([72.0] * 25),
                             "MA12": _daily([60.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(20.0, abs=1e-9)
        assert f["score"] == 2

    def test_below_long_ma_scores_down(self):
        """低于长期均线（周期底部区域）→ 负分（B 轴低 = 价格低位）。"""
        f = _run(StubEngine({"IDX": _daily([55.0] * 25),
                             "MA12": _daily([60.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(-8.33, abs=0.01)
        assert f["score"] == -2

    def test_raw_metric_is_the_ma_value(self):
        r = _run(StubEngine({"IDX": _daily([72.0] * 25),
                             "MA12": _daily([60.0] * 25)}), self._defn())
        assert r["metric_values"]["ma12_level"] == pytest.approx(60.0, abs=1e-9)


# ---------------------------------------------------------------------------
# pct_rank_level（猪价历史分位）—— hog 的 ④ 与 cu 的 ⑤ 符号相反
# ---------------------------------------------------------------------------

_PRICE_RANK_FACTOR = [{
    "key": "rank", "compute": "pct_rank_level", "name": "④ 猪价历史分位",
    "series": "sp", "metric": "price_rank_pct", "raw_metric": "price_level",
    "lookback_periods": 60, "min_samples": 30, "no_series_rank": True,
    "unit": "%分位", "label": "生猪现货成交均价", "thresholds_key": "t", "source": "测试",
}]
_PRICE_RANK_TH = {"t": [[-9999, 20, -2], [20, 40, -1], [40, 60, 0], [60, 80, 1], [80, 9999, 2]]}


class TestHogPriceRank:
    def _defn(self):
        return _defn(_PRICE_RANK_FACTOR, {"sp": {"kind": "macro", "code": "SP"}},
                     _PRICE_RANK_TH)

    def test_low_price_percentile_scores_down(self):
        """⭐ 与 cu 的 ⑤ 相反：价格低分位 → **负分**（B 轴正分 = 价格高位）。

        原始 hog_regime.py 的判据是 `if B <= -2: 价格低位`——B 低对应价格低。
        若照抄 cu 的阈值，这条会变成 +2，「左侧布局期」也就无从判定。
        """
        vals = [100.0 - i for i in range(80)]
        f = _run(StubEngine({"SP": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] < 20
        assert f["score"] == -2

    def test_high_price_percentile_scores_up(self):
        vals = [100.0 + i for i in range(80)]
        f = _run(StubEngine({"SP": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] > 80
        assert f["score"] == 2


# ---------------------------------------------------------------------------
# spread_pct（期现价差）—— 升水为正
# ---------------------------------------------------------------------------

_BASIS_FACTOR = [{
    "key": "basis", "compute": "spread_pct", "name": "⑤ 期现价差",
    "a": "fut", "b": "spot_price", "metric": "basis_pct",
    "unit": "%", "label_a": "期货", "label_b": "现货",
    "thresholds_key": "t", "source": "测试",
}]
_BASIS_TH = {"t": [[-9999, -5, -1], [-5, 10, 0], [10, 9999, 1]]}


class TestHogBasis:
    def _defn(self):
        return _defn(_BASIS_FACTOR,
                     {"fut": {"kind": "macro", "code": "F"},
                      "spot_price": {"kind": "macro", "code": "SP"}},
                     _BASIS_TH)

    def test_premium_scores_up(self):
        """期货升水 15% → +1：市场已把反转定价进去了（B 轴走高）。"""
        f = _run(StubEngine({"F": _daily([11.5] * 25),
                             "SP": _daily([10.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(15.0, abs=1e-9)
        assert f["score"] == 1

    def test_discount_scores_down(self):
        f = _run(StubEngine({"F": _daily([9.0] * 25),
                             "SP": _daily([10.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(-10.0, abs=1e-9)
        assert f["score"] == -1

    def test_weekly_spot_vs_daily_futures_aligns(self):
        """现货周频、期货日频 → 按共同日对齐（本台账实测共同日仅约 48 个）。"""
        base = date(2026, 9, 25)
        fut = [{"report_date": (base - timedelta(days=10 - i)).isoformat(),
                "value": 11.5} for i in range(11)]
        spot = [{"report_date": (base - timedelta(days=10 - i)).isoformat(),
                 "value": 10.0} for i in (0, 7)]
        spot.append({"report_date": (base - timedelta(days=1)).isoformat(), "value": 10.0})
        f = _run(StubEngine({"F": fut, "SP": spot}), self._defn())["factors"][0]
        assert f["asof"] == (base - timedelta(days=1)).isoformat()
        assert f["value"] == pytest.approx(15.0, abs=1e-9)


# ---------------------------------------------------------------------------
# pct_change_window（出栏均重）—— 越降越有利
# ---------------------------------------------------------------------------

_WEIGHT_FACTOR = [{
    "key": "weight", "compute": "pct_change_window", "name": "③ 出栏均重",
    "series": "w", "metric": "weight_chg_pct", "raw_metric": "weight_level",
    "unit": "%", "raw_unit": "kg", "label": "出栏均重",
    "window_label": "个交易日", "lookback_periods": 20,
    "thresholds_key": "t", "source": "测试",
}]
_WEIGHT_TH = {"t": [[-9999, -1, 1], [-1, 1, 0], [1, 9999, -1]]}


class TestHogWeight:
    def _defn(self):
        return _defn(_WEIGHT_FACTOR, {"w": {"kind": "macro", "code": "W"}}, _WEIGHT_TH)

    def test_falling_weight_scores_up(self):
        """均重下降 2% → +1：主动去化/提前出栏的行为信号。"""
        vals = [130.0] * 20 + [127.4]
        f = _run(StubEngine({"W": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(-2.0, abs=0.01)
        assert f["score"] == 1

    def test_rising_weight_scores_down(self):
        """均重上升 → 压栏/二次育肥回升，透支远期供给 → −1。"""
        vals = [128.0] * 20 + [131.0]
        f = _run(StubEngine({"W": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(2.34, abs=0.01)
        assert f["score"] == -1

    def test_flat_weight_is_neutral(self):
        f = _run(StubEngine({"W": _daily([128.0] * 21)}), self._defn())["factors"][0]
        assert f["score"] == 0


# ---------------------------------------------------------------------------
# 全定义离线跑通：真实 hog_regime.json + 合成数据
# ---------------------------------------------------------------------------

def _hog_stub_series():
    """构造一份能跑通全部 6 因子的合成序列。

    ④ 猪价分位要 60 期以上、⑥ 要 index/ma12 两条腿、⑤ 要期货与现货的共同日。
    这里做成「价格低位」形态：猪价长期缓跌到低位 → ④ 低分位（−2）、
    ⑥ 低于均线（负）、① 猪粮比低（正）——即左侧布局的典型数据面貌。
    """
    n = 700
    base = date(2026, 9, 24)
    price_rows, index_rows, ma_rows, weight_rows = [], [], [], []
    for i in range(n):
        d = (base - timedelta(days=n - 1 - i)).isoformat()
        # 成交均价：从 18 元缓跌到 10 元左右（末端低位）
        p = 18.0 - 8.0 * (i / (n - 1)) + 0.5 * math.sin(i / 30.0)
        price_rows.append({"report_date": d, "value": round(p, 3)})
        # 指数与 12 月均线：指数跟随价格，均线滞后 → 末端低于均线
        idx = 90.0 - 30.0 * (i / (n - 1)) + 1.0 * math.sin(i / 30.0)
        index_rows.append({"report_date": d, "value": round(idx, 2)})
        ma = 95.0 - 22.0 * (i / (n - 1))
        ma_rows.append({"report_date": d, "value": round(ma, 2)})
        # 出栏均重：缓慢下降（主动去化）
        w = 132.0 - 3.0 * (i / (n - 1)) + 0.3 * math.sin(i / 20.0)
        weight_rows.append({"report_date": d, "value": round(w, 2)})

    hog = _daily([12.0] * 20 + [11.4])
    corn = _daily([2.55] * 21)
    fut = _daily([12.0] * 20 + [12.6])

    return {"hog_spot_price": price_rows, "hog_spot_weight": weight_rows,
            "hog_spot_index": index_rows, "hog_spot_ma12": ma_rows,
            "hog_lean_spot": hog, "corn_spot": corn, "hog_fut_main": fut}


class TestHogFullDefinitionOffline:
    @pytest.fixture(scope="class")
    def result(self):
        return ThesisEngine.compute(StubEngine(_hog_stub_series()), "hog_regime")

    def test_all_six_factors_valid(self, result):
        assert result["valid_count"] == result["factor_count"] == 6
        # 手工项 seed 数据日期是 2026-06-30，距今 87 天 < 120 → 不该告警
        assert result["warnings"] == [], result["warnings"]

    def test_total_is_the_sum_of_factor_scores(self, result):
        assert result["total"] == sum(f["score"] for f in result["factors"])

    def test_stage_block_present_and_consistent(self, result):
        st = result["stages"]
        assert st is not None and st["stage"]
        assert st["drive_full"] == 5 and st["pos_full"] == 5
        assert st["drive_valid_count"] == st["drive_factor_count"] == 3
        assert st["pos_valid_count"] == st["pos_factor_count"] == 3
        assert st["complete"] is True
        # B 轴是 3 项求和
        pos_sum = sum(f["score"] for f in result["factors"]
                      if f["key"] in st["pos_keys"])
        assert st["pos_total"] == pos_sum == st["pos_score"]

    def test_stage_is_left_side_layout_with_this_data(self, result):
        """合成数据刻意构造成「出清偏充分 + 价格低位」→ 应落在左下角的左侧布局期一带。

        不钉死具体阶段名（数值随夹具微调而变），但钉住「价格轴必须在低位区间」——
        即 pos_total 为负。这直接验证 B 轴极性：价格低位 ⇒ B 负。
        """
        st = result["stages"]
        assert st["pos_total"] < 0
        assert st["pos_band"] == "价格低位"

    def test_stages_do_not_enter_total(self, result):
        st = result["stages"]
        assert result["total"] == sum(f["score"] for f in result["factors"]
                                      if f["score"] is not None)
        assert result["total"] == st["drive_total"] + st["pos_total"]

    def test_sow_factor_is_manual_and_scored_on_deviation(self, result):
        f = [x for x in result["factors"] if x["key"] == "sow"][0]
        assert f["manual"] is True
        assert 7 < f["value"] < 8          # 较高点 4080 去化约 7.35%
        assert f["score"] == 2
        assert result["metric_values"]["sow_level"] == 3780
        assert result["metric_values"]["sow_stale"] == 0

    def test_signals_and_invalidation_reference_known_metrics(self, result):
        for group in ("buy", "warn"):
            for item in result["signals"][group]:
                assert item["label"]
                assert item["current"] is not None, item
        for rule in result["invalidation"]:
            assert rule["text"]
            assert rule["current"] is not None, rule
        for k in ("hog_corn_ratio", "sow_dev_pct", "sow_stale", "weight_chg_pct",
                  "price_rank_pct", "basis_pct", "vs_ma12_pct", "sow_level"):
            assert k in result["metric_values"], k

    def test_sow_stale_signal_can_fire(self, result):
        """sow_stale 是 is_true 型信号：当前未过期 → 该条 hit=False，current='正常'。"""
        item = [i for i in result["signals"]["warn"] if "能繁" in i["label"]][0]
        assert item["hit"] is False
        assert item["current"] == "正常"
        # 且该条不是「数据缺失」——current 必须是有意义的读数
        assert item["current"] is not None

    def test_scenarios_and_transmission_evaluated(self, result):
        assert len(result["scenarios"]) == 3
        assert len(result["transmission"]) == 4
        for s in result["scenarios"]:
            assert s["conditions"], s["name"]
        assert any(p["current"] for p in result["phases"])

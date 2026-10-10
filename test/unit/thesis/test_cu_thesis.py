"""铜周期台账（cu_regime）：四个新因子计算器 + pos_keys 双维阶段矩阵，完全离线。

为什么单独一个文件：
1. **±2 硬上限的保序压缩是本台账的立身之本**。原始 `cu_regime.py` 用 ±3 制
   （美元/螺纹钢/铜价分位/动量分位都是 ±3），而引擎的 `SCORE_MIN/SCORE_MAX` 是 ±2
   且被 `test_thesis_engine.py::test_appreciation_is_capped_at_two` 锁定。台账做了
   「3/2/0/−2/−3 → 2/1/0/−1/−2」的**保序压缩**——档位边界原样保留，只压分值刻度。
   一旦有人「顺手把 3 改回来」，引擎会在 `engine.py` 里静默 clamp 掉，
   阶段档位与 verdicts 全部失真。这里逐张表把「得分 ∈ [-2,2]」钉死。
2. **B 轴方向与黄金相反（更易错）**：黄金 B 轴「正分 = 便宜」，铜同口径；但铜的
   ⑦ 动量分位与 ⑧ 铜金比是**逆周期取向**——动量分位高给负分、铜金比上升给正分。
   而猪周期台账的 B 轴又是「正分 = 价格高位」。三张台账三种符号约定，
   最容易出的错就是「照抄上一张的阈值符号」，故这里把每条的方向显式断言出来。
3. **两条腿日期不齐**：⑧ 铜金比的两条腿（沪铜日频、上海金日频）最后一天未必相同。
   各取最新一行会得到看似合理但错的比值——夹具里故意让两条腿错位，把对齐逻辑钉死。
4. **铜与美债同向**：② 美债 10Y 的符号与黄金台账**完全相反**（黄金 r=−0.261，
   铜 r=+0.170）。这是资产属性差异（分母端 vs 分子端），不是数据错误。
   若有人按「黄金的阈值表抄一份」，这条会静默反向，故显式断言。
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
CU_DEF = ROOT / "data" / "thesis" / "cu_regime.json"


def _daily(values, end=None):
    """按自然日倒推生成日频序列（升序）。"""
    end = end or date(2026, 9, 24)
    n = len(values)
    return [{"report_date": (end - timedelta(days=n - 1 - i)).isoformat(), "value": float(v)}
            for i, v in enumerate(values)]


def _defn(factors, data, thresholds, stages=None, params=None, verdicts=None):
    d = {
        "id": "t_cu", "name": "铜测试", "version": "1.0",
        "subject": {"index_code": "CU0", "benchmark_code": "RB0"},
        # data 必须声明：漏了 _prepare 不往 ctx 放序列 → 所有因子判「数据缺失」
        "data": data,
        "factors": factors,
        "params": params or {},
        "thresholds": thresholds,
        "verdicts": verdicts or [{"min_total": -99, "verdict": "x", "action": "y"}],
    }
    if stages is not None:
        d["stages"] = stages
    return d


class StubEngine(ThesisEngine):
    """取数层换内存数据；按 code 分派，未提供的 code 返回空序列。"""

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

class TestCuDefinition:
    @pytest.fixture(scope="class")
    def defn(self):
        return json.loads(CU_DEF.read_text(encoding="utf-8"))

    def test_loads_and_validates(self):
        d, problems = defs.load_def_checked("cu_regime")
        assert d is not None
        assert problems == [], problems

    def test_listed(self):
        assert "cu_regime" in defs.list_defs()

    def test_every_compute_is_registered(self, defn):
        for f in defn["factors"]:
            assert f["compute"] in ThesisEngine.FACTOR_COMPUTERS, f["compute"]

    def test_every_threshold_key_exists(self, defn):
        for f in defn["factors"]:
            if f.get("thresholds_key"):
                assert f["thresholds_key"] in defn["thresholds"], f["key"]

    def test_scores_never_exceed_the_engine_cap(self, defn):
        """±2 是引擎硬上限（SCORE_MIN/SCORE_MAX），任何一张表都不许越界。

        原始 cu_regime.py 是 ±3 制，台账做的是**保序压缩**；若有人把 3 改回来，
        引擎在 `engine.py:434/438/479/482` 会静默 clamp 成 2 —— 档位边界随之失真，
        而表面上不报任何错。这条断言就是那道闸门。
        """
        for name, table in defn["thresholds"].items():
            for lo, hi, sc in table:
                assert defs.SCORE_MIN <= sc <= defs.SCORE_MAX, (name, sc)

    def test_threshold_tables_cover_the_whole_range(self, defn):
        """阈值没兜住 ±99 时越界值会**静默变「数据缺失」**（SOP 已踩两次）。"""
        for name, table in defn["thresholds"].items():
            assert min(r[0] for r in table) <= -99, name
            assert max(r[1] for r in table) >= 99, name

    def test_drive_full_matches_the_actual_achievable_max(self, defn):
        """drive_full 必须等于「各驱动力因子的最大得分之和」，不是「因子数 × 2」。"""
        st = defn["stages"]
        by_key = {f["key"]: f for f in defn["factors"]}
        real_max = 0
        for k in st["drive_keys"]:
            table = defn["thresholds"][by_key[k]["thresholds_key"]]
            real_max += max(r[2] for r in table)
        assert st["drive_full"] == real_max == 8

    def test_pos_full_matches_the_actual_achievable_max(self, defn):
        """位置轴是 pos_keys 多项求和，pos_full 必须等于这几项的现实最大值之和。"""
        st = defn["stages"]
        by_key = {f["key"]: f for f in defn["factors"]}
        real_max = 0
        for k in st["pos_keys"]:
            table = defn["thresholds"][by_key[k]["thresholds_key"]]
            real_max += max(r[2] for r in table)
        assert st["pos_full"] == real_max == 8

    def test_position_keys_are_not_in_the_drive_axis(self, defn):
        """两条轴必须正交：位置因子不得出现在 drive_keys 里。"""
        st = defn["stages"]
        assert not (set(st["pos_keys"]) & set(st["drive_keys"]))
        keys = {f["key"] for f in defn["factors"]}
        assert set(st["pos_keys"]) <= keys

    def test_every_factor_belongs_to_exactly_one_axis(self, defn):
        """8 个因子必须恰好覆盖两条轴，不许有「不上轴」的游离因子。"""
        st = defn["stages"]
        mine = set(st["drive_keys"]) | set(st["pos_keys"])
        assert mine == {f["key"] for f in defn["factors"]}

    def test_stage_cells_cover_all_nine_combinations(self, defn):
        st = defn["stages"]
        need = {(d["name"], p["name"])
                for d in st["drive_bands"] for p in st["pos_bands"]}
        got = {(c["drive"], c["pos"]) for c in st["cells"]}
        assert got == need, need - got
        # 阶段名必须唯一，否则页面上无法定位「当前格」
        names = [c["stage"] for c in st["cells"]]
        assert len(names) == len(set(names)) == 9

    def test_verdicts_ladder_is_strictly_descending_and_bottomed(self, defn):
        mins = [v["min_total"] for v in defn["verdicts"]]
        assert mins == sorted(mins, reverse=True)
        assert mins[-1] <= -99
        # 最高档不能超过两轴现实满分的和（8+8=16），否则永远摸不到
        assert mins[0] <= 16

    def test_position_axis_positive_means_cheap(self, defn):
        """⭐ B 轴方向约定：cu 的正分 = 价格低位/便宜（与 gold_regime 同口径）。

        证据：⑤ 位置分位与 ⑦ 动量分位两张表都是「低分位 → 正分」。
        若有人把它改成「高分位 → 正分」（照抄猪台账），B 轴语义会整体翻转，
        「中性高位期」会变成「中性低位期」，正是本台账最想区分的那个对立面。
        """
        assert defn["thresholds"]["cu_rank_pct"][0][2] == 2     # 低分位 = 便宜 = +2
        assert defn["thresholds"]["cu_rank_pct"][-1][2] == -2   # 高分位 = 贵 = −2
        assert defn["thresholds"]["mom_rank_pct"][0][2] == 2
        assert defn["thresholds"]["mom_rank_pct"][-1][2] == -2

    def test_us10_sign_is_opposite_to_gold(self, defn):
        """⭐ ② 美债 10Y：铜与黄金**符号相反**（铜 r=+0.170 / 黄金 r=−0.261）。

        铜是分子端资产（利率上行 = 经济预期改善 = 拉动），黄金是分母端
        （利率上行 = 机会成本上升 = 压制）。若照抄黄金的阈值表，这条会静默反向。
        """
        t = defn["thresholds"]["us10_chg_bp"]
        assert t[0][2] == -2 and t[-1][2] == 2   # 利率上行 → 正分（铜）

    def test_dollar_and_steel_rebar_signs(self, defn):
        """① 美元走弱有利（分母端，与黄金同向）；④ 螺纹钢走强有利（最强正相关）。"""
        d = defn["thresholds"]["dxy_chg_bp"]
        assert d[0][2] == 2 and d[-1][2] == -2
        r = defn["thresholds"]["rb_chg_pct"]
        assert r[0][2] == -2 and r[-1][2] == 2

    def test_cuau_is_pro_cyclical_not_contrarian(self, defn):
        """⑧ 铜金比：**上升有利**（实体接棒），与 ⑤⑥⑦ 的逆周期相反。

        这是全台账唯一「正分朝上」的位置动量因子。照抄金银比（回落有利）会反向。
        """
        c = defn["thresholds"]["cuau_chg_pct"]
        assert c[0][2] == -2 and c[-1][2] == 2


# ---------------------------------------------------------------------------
# ratio_level —— 猪粮比代理（比值本身作读数；这里用铜/螺纹作同构用例）
# ---------------------------------------------------------------------------

_RATIO_LEVEL_FACTOR = [{
    "key": "rl", "compute": "ratio_level", "name": "① 比值水平",
    "a": "num", "b": "den", "metric": "x_ratio",
    "unit": "", "label_a": "分子", "label_b": "分母",
    "thresholds_key": "t", "source": "测试",
}]
_RATIO_LEVEL_TH = {"t": [[-9999, 5, 2], [5, 6, 1], [6, 7, 0], [7, 8, -1], [8, 9999, -2]]}


class TestRatioLevel:
    def _defn(self):
        return _defn(_RATIO_LEVEL_FACTOR,
                     {"num": {"kind": "macro", "code": "NUM"},
                      "den": {"kind": "macro", "code": "DEN"}},
                     _RATIO_LEVEL_TH)

    def test_scores_the_ratio_itself(self):
        """阈值直接作用在比值上（5:1 是官方预警线），不是变动率。"""
        f = _run(StubEngine({"NUM": _daily([10.0] * 25),
                             "DEN": _daily([2.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(5.0, abs=1e-9)
        assert f["score"] == 1          # [5, 6) → 1
        assert f["unit"] == ""

    def test_alignment_uses_common_dates(self):
        """两条腿日期不齐时必须按共同日配对，不能用各自的最后一行。

        夹具里分子多两个交易日且末端跳涨（10 → 20），分母只到倒数第三日。
        错用「各取最新一行」会得到 20/2 = 10（虚假高比值）。
        """
        n = 25
        base = date(2026, 9, 20)
        num = [{"report_date": (base - timedelta(days=n - 1 - i)).isoformat(),
                "value": 10.0} for i in range(n)]
        num += [{"report_date": "2026-09-21", "value": 15.0},
                {"report_date": "2026-09-22", "value": 20.0}]
        den = [{"report_date": (base - timedelta(days=n - 1 - i)).isoformat(),
                "value": 2.0} for i in range(n)]
        r = _run(StubEngine({"NUM": num, "DEN": den}), self._defn())
        f = r["factors"][0]
        assert f["asof"] == "2026-09-20"
        assert f["value"] == pytest.approx(5.0, abs=1e-9)
        assert r["metric_values"]["x_ratio"] == pytest.approx(5.0, abs=1e-9)

    def test_no_common_date_is_missing_not_neutral(self):
        num = _daily([10.0] * 5, end=date(2026, 9, 22))
        den = _daily([2.0] * 5, end=date(2026, 8, 1))
        f = _run(StubEngine({"NUM": num, "DEN": den}), self._defn())["factors"][0]
        assert f["value"] is None and f["score"] is None
        assert "无共同日期" in f["detail"]

    def test_zero_denominator_is_missing(self):
        f = _run(StubEngine({"NUM": _daily([10.0] * 25),
                             "DEN": _daily([0.0] * 25)}), self._defn())["factors"][0]
        assert f["score"] is None and "分母" in f["detail"]

    def test_stale_marked_when_common_date_is_old(self):
        """陈旧判定看**共同日期**（asof），不是看某一条腿的序列长度。

        夹具：两条腿都有 25 期，但都在 8 月中旬结束 → 共同日距 TODAY 超过 10 天
        → stale=True。这个断层容易踩：若实现改成「只看分子腿」，两条腿新旧不一时
        会漏报。
        """
        old_end = date(2026, 8, 20)
        num = _daily([10.0] * 25, end=old_end)
        den = _daily([2.0] * 25, end=old_end)
        f = _run(StubEngine({"NUM": num, "DEN": den}), self._defn())["factors"][0]
        assert f["asof"] == "2026-08-20"
        assert f["value"] == pytest.approx(5.0, abs=1e-9)
        assert f["stale"] is True

    def test_fresh_common_date_is_not_stale(self):
        """两条腿都新鲜 → 不标陈旧（对照组，防止上面那条断言恒真）。"""
        num = _daily([10.0] * 25, end=date(2026, 9, 24))
        den = _daily([2.0] * 25, end=date(2026, 9, 24))
        f = _run(StubEngine({"NUM": num, "DEN": den}), self._defn())["factors"][0]
        assert f["stale"] is False


# ---------------------------------------------------------------------------
# pct_rank_level —— 猪价历史分位（读数自身取分位，不去趋势）
# ---------------------------------------------------------------------------

_PCT_RANK_FACTOR = [{
    "key": "pr", "compute": "pct_rank_level", "name": "④ 价格分位",
    "series": "p", "metric": "price_rank_pct", "raw_metric": "price_level",
    "unit": "%分位", "label": "价格", "lookback_periods": 60, "min_samples": 30,
    "no_series_rank": True, "thresholds_key": "t", "source": "测试",
}]
_PCT_RANK_TH = {"t": [[-9999, 20, -2], [20, 40, -1], [40, 60, 0], [60, 80, 1], [80, 9999, 2]]}


class TestPctRankLevel:
    def _defn(self):
        return _defn(_PCT_RANK_FACTOR, {"p": {"kind": "macro", "code": "P"}}, _PCT_RANK_TH)

    def test_high_price_lands_in_the_top_band(self):
        """价格单调上行 → 末端是历史最高 → 分位 ~100% → 高分位 → +2（价格高位）。"""
        vals = [100.0 + i for i in range(80)]
        f = _run(StubEngine({"P": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] > 80
        assert f["score"] == 2
        assert f["pct_rank"] is None and f["pct_rank_off"] is True

    def test_low_price_lands_in_the_bottom_band(self):
        vals = [200.0 - i for i in range(80)]
        f = _run(StubEngine({"P": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] < 20
        assert f["score"] == -2

    def test_insufficient_sample_is_missing(self):
        f = _run(StubEngine({"P": _daily([100.0] * 10)}), self._defn())["factors"][0]
        assert f["score"] is None and "样本不足" in f["detail"]

    def test_raw_metric_is_the_price_itself(self):
        vals = [100.0 + i for i in range(80)]
        r = _run(StubEngine({"P": _daily(vals)}), self._defn())
        assert r["metric_values"]["price_level"] == pytest.approx(179.0, abs=1e-9)


# ---------------------------------------------------------------------------
# spread_pct —— 期现价差（不同频对齐 + 升水为正）
# ---------------------------------------------------------------------------

_SPREAD_PCT_FACTOR = [{
    "key": "sp", "compute": "spread_pct", "name": "⑤ 期现价差",
    "a": "fut", "b": "spot", "metric": "basis_pct",
    "unit": "%", "label_a": "期货", "label_b": "现货",
    "thresholds_key": "t", "source": "测试",
}]
_SPREAD_PCT_TH = {"t": [[-9999, -5, -1], [-5, 10, 0], [10, 9999, 1]]}


class TestSpreadPct:
    def _defn(self):
        return _defn(_SPREAD_PCT_FACTOR,
                     {"fut": {"kind": "macro", "code": "F"},
                      "spot": {"kind": "macro", "code": "S"}},
                     _SPREAD_PCT_TH)

    def test_premium_is_positive(self):
        """期货升水 = 市场已确认反转 → 正分（与 cu 的「贴水有利」相反）。"""
        f = _run(StubEngine({"F": _daily([13.0] * 25),
                             "S": _daily([10.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(30.0, abs=1e-9)
        assert f["score"] == 1
        assert "升水" in f["detail"]

    def test_discount_is_negative(self):
        f = _run(StubEngine({"F": _daily([9.0] * 25),
                             "S": _daily([10.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(-10.0, abs=1e-9)
        assert f["score"] == -1
        assert "贴水" in f["detail"]

    def test_flat_is_neutral(self):
        f = _run(StubEngine({"F": _daily([10.0] * 25),
                             "S": _daily([10.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == 0.0 and f["score"] == 0
        assert "平水" in f["detail"]

    def test_cross_frequency_alignment_uses_common_dates(self):
        """期货日频、现货周频：必须按共同日对齐，否则拿 09-28 的期货减 09-21 的现货。"""
        base = date(2026, 9, 25)
        fut = [{"report_date": (base - timedelta(days=10 - i)).isoformat(),
                "value": 13.0} for i in range(11)]
        # 现货只有间歇的几期（周频），最后一期在 base-2
        spot = [{"report_date": (base - timedelta(days=10 - i)).isoformat(),
                 "value": 10.0} for i in (0, 3, 6)]
        spot.append({"report_date": (base - timedelta(days=2)).isoformat(), "value": 10.0})
        f = _run(StubEngine({"F": fut, "S": spot}), self._defn())["factors"][0]
        assert f["asof"] == (base - timedelta(days=2)).isoformat()
        assert f["value"] == pytest.approx(30.0, abs=1e-9)

    def test_no_common_date_is_missing(self):
        f = _run(StubEngine({"F": _daily([13.0] * 5, end=date(2026, 9, 20)),
                             "S": _daily([10.0] * 5, end=date(2026, 8, 1))}),
                 self._defn())["factors"][0]
        assert f["score"] is None and "无共同日期" in f["detail"]


# ---------------------------------------------------------------------------
# vs_ma_gap —— 相对源自带均线列的偏离（现货 vs 12 月均线）
# ---------------------------------------------------------------------------

_VS_MA_GAP_FACTOR = [{
    "key": "mg", "compute": "vs_ma_gap", "name": "⑥ 现货 vs 均线",
    "a": "idx", "b": "ma", "metric": "vs_ma_pct", "raw_metric": "ma_level",
    "unit": "%", "label": "现货指数", "thresholds_key": "t", "source": "测试",
}]
_VS_MA_GAP_TH = {"t": [[-9999, -5, -2], [-5, 0, -1], [0, 5, 0], [5, 15, 1], [15, 9999, 2]]}


class TestVsMaGap:
    def _defn(self):
        return _defn(_VS_MA_GAP_FACTOR,
                     {"idx": {"kind": "macro", "code": "IDX"},
                      "ma": {"kind": "macro", "code": "MA"}},
                     _VS_MA_GAP_TH)

    def test_above_ma_is_positive(self):
        """高于长期均线（景气区间）= 正分——B 轴「正分 = 价格高位」。"""
        f = _run(StubEngine({"IDX": _daily([72.0] * 25),
                             "MA": _daily([60.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(20.0, abs=1e-9)
        assert f["score"] == 2

    def test_below_ma_is_negative(self):
        f = _run(StubEngine({"IDX": _daily([55.0] * 25),
                             "MA": _daily([60.0] * 25)}), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(-8.33, abs=0.01)
        assert f["score"] == -2

    def test_series_alias_fallback(self):
        """spec['a'] 缺省时应回退到 spec['series']（老口径兼容）。"""
        factor = [dict(_VS_MA_GAP_FACTOR[0])]
        factor[0].pop("a")
        factor[0]["series"] = "idx"
        d = _defn(factor, {"idx": {"kind": "macro", "code": "IDX"},
                           "ma": {"kind": "macro", "code": "MA"}}, _VS_MA_GAP_TH)
        f = _run(StubEngine({"IDX": _daily([72.0] * 25),
                             "MA": _daily([60.0] * 25)}), d)["factors"][0]
        assert f["value"] == pytest.approx(20.0, abs=1e-9)

    def test_aligns_on_common_dates(self):
        idx = _daily([72.0] * 25, end=date(2026, 9, 24))
        ma = _daily([60.0] * 25, end=date(2026, 9, 22))
        f = _run(StubEngine({"IDX": idx, "MA": ma}), self._defn())["factors"][0]
        assert f["asof"] == "2026-09-22"
        assert f["value"] == pytest.approx(20.0, abs=1e-9)

    def test_zero_ma_is_missing(self):
        f = _run(StubEngine({"IDX": _daily([72.0] * 25),
                             "MA": _daily([0.0] * 25)}), self._defn())["factors"][0]
        assert f["score"] is None and "均线读数为 0" in f["detail"]


# ---------------------------------------------------------------------------
# vs_ma_rank —— 相对自算均线的偏离（铜价 vs 年均线）
# ---------------------------------------------------------------------------

_VS_MA_RANK_FACTOR = [{
    "key": "mr", "compute": "vs_ma_rank", "name": "⑥ 铜价 vs 年均线",
    "series": "cu", "metric": "ma_gap_pct", "raw_metric": "ma_level",
    "ma_days": 20, "unit": "%", "label": "沪铜主连",
    "thresholds_key": "t", "source": "测试",
}]
_VS_MA_RANK_TH = {"t": [[-9999, -10, 2], [-10, 0, 1], [0, 10, 0], [10, 20, -1], [20, 9999, -2]]}


class TestVsMaRank:
    def _defn(self):
        return _defn(_VS_MA_RANK_FACTOR, {"cu": {"kind": "macro", "code": "CU"}},
                     _VS_MA_RANK_TH)

    def test_far_above_ma_scores_down(self):
        """铜的 ⑥ 是逆周期：越贵（越高于均线）越不利。"""
        vals = [100.0] * 25 + [150.0]        # 末端远超 20 日均线
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] > 20
        assert f["score"] == -2

    def test_far_below_ma_scores_up(self):
        vals = [100.0] * 25 + [60.0]
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] < -10
        assert f["score"] == 2

    def test_short_history_is_missing(self):
        f = _run(StubEngine({"CU": _daily([100.0] * 10)}), self._defn())["factors"][0]
        assert f["score"] is None and "历史不足" in f["detail"]

    def test_ma_days_comes_from_spec(self):
        """ma_days 优先取 spec，而非 params——两条均线口径（250/20）靠它区分。"""
        vals = [100.0] * 40
        vals[-1] = 200.0
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        # 20 日均线：其余 19 个 100 + 1 个 200 → 均线 105；200/105−1 = +90.5%
        assert f["value"] == pytest.approx(90.48, abs=0.1)


# ---------------------------------------------------------------------------
# momentum_rank —— 20 日动量分位
# ---------------------------------------------------------------------------

_MOM_FACTOR = [{
    "key": "mom", "compute": "momentum_rank", "name": "⑦ 动量分位",
    "series": "cu", "metric": "mom_rank_pct", "raw_metric": "mom_chg_pct",
    "lookback_periods": 20, "min_samples": 50,
    "unit": "%分位", "label": "沪铜主连", "no_series_rank": True,
    "thresholds_key": "t", "source": "测试",
}]
_MOM_TH = {"t": [[-9999, 20, 2], [20, 40, 1], [40, 60, 0], [60, 80, -1], [80, 9999, -2]]}


class TestMomentumRank:
    def _defn(self):
        return _defn(_MOM_FACTOR, {"cu": {"kind": "macro", "code": "CU"}}, _MOM_TH)

    def test_reads_the_percentile_not_the_return(self):
        vals = [100.0] * 200 + [100.0 + i for i in range(1, 21)]
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        assert 0 <= f["value"] <= 100
        assert f["pct_rank"] is None and f["pct_rank_off"] is True

    def test_strong_momentum_scores_down(self):
        """逆周期：动量极端（末端加速）→ 高分位 → −2。"""
        vals = [100.0] * 200 + [100.0 * (1.02 ** i) for i in range(1, 21)]
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] > 80
        assert f["score"] == -2

    def test_negative_momentum_scores_up(self):
        vals = [100.0] * 200 + [100.0 * (0.98 ** i) for i in range(1, 21)]
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] < 20
        assert f["score"] == 2

    def test_short_history_is_missing(self):
        f = _run(StubEngine({"CU": _daily([100.0] * 30)}), self._defn())["factors"][0]
        assert f["score"] is None and "样本不足" in f["detail"]

    def test_insufficient_momentum_samples_is_missing(self):
        """有 21 期能算出 1 个动量，但 < min_samples(50) → 数据缺失，不给 0 分。"""
        vals = [100.0] * 40
        f = _run(StubEngine({"CU": _daily(vals)}), self._defn())["factors"][0]
        assert f["score"] is None and "样本不足" in f["detail"]


# ---------------------------------------------------------------------------
# 双向极性对照：cu 的 B 轴 = 便宜为正；hog 语义相反（跨文件互斥由注释约束）
# ---------------------------------------------------------------------------

class TestPositionPolarityContract:
    def test_cu_score_signs_are_internally_consistent(self):
        """cu 的四个位置动量因子中，⑤⑥⑦ 低/便宜给正分、⑧ 上升给正分。

        该断言的意义不是「描述事实」，而是**钉住约定**：
        任何一次「把阈值表换成猪周期的」都会在这里立刻红。
        """
        d = json.loads(CU_DEF.read_text(encoding="utf-8"))
        th = d["thresholds"]
        assert th["cu_rank_pct"][0][2] == 2        # 低位 +2
        assert th["vs_ma_pct"][0][2] == 2          # 低于均线 +2
        assert th["mom_rank_pct"][0][2] == 2       # 低动量 +2
        assert th["cuau_chg_pct"][-1][2] == 2      # 铜金比上升 +2（唯一顺周期）


# ---------------------------------------------------------------------------
# 全定义离线跑通：真实 cu_regime.json + 合成数据
# ---------------------------------------------------------------------------

def _cu_stub_series():
    """构造一份能跑通全部 8 因子的合成序列（含 1400 日铜价以满足 250 期要求）。"""
    n = 1400
    base = date(2026, 9, 24)
    cu_rows, rb_rows, au_rows = [], [], []
    for i in range(n):
        d = (base - timedelta(days=n - 1 - i)).isoformat()
        # 铜：温和上行 + 周期波动，末端延伸度落在中高位
        cu = 60000.0 + i * 8.0 + 3000.0 * ((i % 120) / 120.0)
        cu_rows.append({"report_date": d, "value": round(cu, 2)})
        # 螺纹钢：与铜同源波动（最强正相关），单独给一点噪声
        rb = 3400.0 + i * 0.35 + 200.0 * ((i % 90) / 90.0)
        rb_rows.append({"report_date": d, "value": round(rb, 2)})
        # 金价：让铜金比有方向性波动，避免 ⑧ 恒定为 0
        au_rows.append({"report_date": d,
                        "value": round(cu / 1000.0 / (7.8 + 0.6 * math.sin(i / 70.0)), 4)})

    dxy = _daily([96.5] * 20 + [98.0])       # 走强 → ① 不利
    us10 = _daily([4.10] * 20 + [4.32])      # 上行 → ② 有利（与黄金相反）
    cn10 = _daily([2.10] * 20 + [2.02])      # 下行 → ③ 不利

    return {"cu_main": cu_rows, "rb_main": rb_rows, "au_sge": au_rows,
            "dxy": dxy, "us10_y": us10, "cn10_y": cn10}


class TestCuFullDefinitionOffline:
    @pytest.fixture(scope="class")
    def result(self):
        # 走真实定义（data/thesis/cu_regime.json）+ 合成数据，完全离线
        return ThesisEngine.compute(StubEngine(_cu_stub_series()), "cu_regime")

    def test_all_eight_factors_valid(self, result):
        assert result["valid_count"] == result["factor_count"] == 8
        assert result["warnings"] == [], result["warnings"]

    def test_total_is_the_sum_of_factor_scores(self, result):
        assert result["total"] == sum(f["score"] for f in result["factors"])

    def test_stage_block_present_and_consistent(self, result):
        st = result["stages"]
        assert st is not None and st["stage"]
        assert st["drive_full"] == 8 and st["pos_full"] == 8
        assert st["drive_valid_count"] == st["drive_factor_count"] == 4
        assert st["pos_valid_count"] == st["pos_factor_count"] == 4
        assert st["complete"] is True
        # 驱动力只累加 4 个驱动力因子
        drive_sum = sum(f["score"] for f in result["factors"]
                        if f["key"] in st["drive_keys"])
        assert st["drive_total"] == drive_sum
        # 位置轴是 pos_keys 求和（不是单因子）
        pos_sum = sum(f["score"] for f in result["factors"]
                      if f["key"] in st["pos_keys"])
        assert st["pos_total"] == pos_sum == st["pos_score"]
        assert st["pos_keys"] == ["rank", "ma", "mom", "cuau"]

    def test_expected_stage_from_the_synthetic_data(self, result):
        """只钉「阶段名与两条轴的档位自洽」，不钉具体数值。"""
        st = result["stages"]
        name_by = {(c["drive"], c["pos"]): c["stage"] for c in st["cells"]}
        assert st["stage"] == name_by[(st["drive_band"], st["pos_band"])]

    def test_stages_do_not_enter_total(self, result):
        """阶段矩阵是研究表达层，不能反过来影响打分。"""
        st = result["stages"]
        assert result["total"] == sum(f["score"] for f in result["factors"]
                                      if f["score"] is not None)
        assert result["total"] == st["drive_total"] + st["pos_total"]

    def test_signals_and_invalidation_reference_known_metrics(self, result):
        for group in ("buy", "warn"):
            for item in result["signals"][group]:
                assert item["label"]
                assert item["current"] is not None, item
        for rule in result["invalidation"]:
            assert rule["text"]
            assert rule["current"] is not None, rule
        # 关键原始读数都进了 metrics
        for k in ("dxy_chg_bp", "us10_chg_bp", "cn10_chg_bp", "rb_chg_pct",
                  "cu_rank_pct", "ma_gap_pct", "mom_rank_pct", "cuau_chg_pct",
                  "rank_ext", "cuau"):
            assert k in result["metric_values"], k

    def test_scenarios_and_transmission_evaluated(self, result):
        assert len(result["scenarios"]) == 4
        assert len(result["transmission"]) == 5
        for s in result["scenarios"]:
            assert s["conditions"], s["name"]
        assert any(p["current"] for p in result["phases"])

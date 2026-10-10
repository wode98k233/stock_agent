"""黄金台账（gold_regime）：四个新因子计算器 + 双维阶段矩阵，完全离线。

为什么单独一个文件：
1. **阶段矩阵是新能力**——它把「驱动力」与「位置」两个正交维度合成九宫格，与一维的
   `verdicts` 是两套语义。最容易出的错是「把 position 也算进驱动力轴」，那样
   「驱动有利 + 位置已高」会被算成「驱动更有利」，正好把报告的立论反过来了。
   这里把两条轴的成员资格显式钉住。
2. **口径错位是这个台账最隐蔽的坑**：金银比的两条腿最后一天不同、实际利率的两条腿
   频率不同（日频 vs 月频）。各取「最新一行」会得到看似合理但错的数——
   这里用「故意让两条腿日期不齐」的夹具把对齐逻辑钉死。
3. **量纲不是 7×2 = ±14**：⑥ 央行储备刻意只有 ±1。有人「顺手统一成 ±2」时，
   drive_full / verdicts 档位会整体漂移，这里用断言拦住。
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
GOLD_DEF = ROOT / "data" / "thesis" / "gold_regime.json"

# 合成测试数据用的月份锚点（与 TODAY 同源派生，避免硬编码日期变成时间炸弹）
M3, M2, M1 = "2026-06", "2026-07", "2026-08"


def _daily(values, end=None):
    """按自然日倒推生成日频序列（升序）。"""
    end = end or date(2026, 9, 24)
    n = len(values)
    return [{"report_date": (end - timedelta(days=n - 1 - i)).isoformat(), "value": float(v)}
            for i, v in enumerate(values)]


def _monthly(pairs):
    """pairs: [(YYYY-MM, 月末日, value)] → 序列。"""
    return [{"report_date": d, "value": float(v)} for _, d, v in pairs]


def _defn(factors, data, thresholds, stages=None, params=None, verdicts=None):
    return {
        "id": "t_gold", "name": "黄金测试", "version": "1.0",
        "subject": {"index_code": "AU9999", "benchmark_code": "DXY"},
        # data 必须声明：漏了 _prepare 不往 ctx 放序列 → 所有因子判「数据缺失」
        "data": data,
        "factors": factors,
        "params": params or {},
        "thresholds": thresholds,
        "verdicts": verdicts or [{"min_total": -99, "verdict": "x", "action": "y"}],
    }


class StubEngine(ThesisEngine):
    """取数层换内存数据；按 code 分派，未提供的 code 返回空序列。"""

    def __init__(self, series=None):
        super().__init__()
        self._series = series or {}

    def today(self):
        return TODAY

    # 月度指标按 45 天判陈旧（真值来自 macro_db.stale_threshold_days）；
    # 一律返回 10 会让「月频腿」在 8 月末的读数被误判陈旧，掩盖 asof 的解析问题的反向情形。
    _MONTHLY_CODES = {"CPI", "us_cpi_yoy", "cn_gold_reserve"}

    def macro_stale_threshold(self, code):
        return 45 if str(code) in self._MONTHLY_CODES else 10

    def macro_series(self, code, periods=60):
        return list(self._series.get(str(code), []))


def _run(engine, defn):
    return engine.compute(defn["id"], defn=defn)


# ---------------------------------------------------------------------------
# 定义本身的守卫
# ---------------------------------------------------------------------------

class TestGoldDefinition:
    @pytest.fixture(scope="class")
    def defn(self):
        return json.loads(GOLD_DEF.read_text(encoding="utf-8"))

    def test_loads_and_validates(self):
        d, problems = defs.load_def_checked("gold_regime")
        assert d is not None
        assert problems == [], problems

    def test_listed(self):
        assert "gold_regime" in defs.list_defs()

    def test_every_compute_is_registered(self, defn):
        for f in defn["factors"]:
            assert f["compute"] in ThesisEngine.FACTOR_COMPUTERS, f["compute"]

    def test_every_threshold_key_exists(self, defn):
        for f in defn["factors"]:
            if f.get("thresholds_key"):
                assert f["thresholds_key"] in defn["thresholds"], f["key"]

    def test_threshold_tables_cover_the_whole_range(self, defn):
        """阈值没兜住 -99~99 时越界值会**静默变「数据缺失」**（SOP 已踩两次）。"""
        for name, table in defn["thresholds"].items():
            assert min(r[0] for r in table) <= -99, name
            assert max(r[1] for r in table) >= 99, name

    def test_cngold_is_deliberately_only_pm1(self, defn):
        """⑥ 央行储备只给 ±1（月度相关性 +0.044，报告定其为不用于择时的慢变量）。

        若有人「顺手统一成 ±2」，驱动力满分就从 11 变成 12，
        stages.drive_full 与 verdicts 的档位含义会整体漂移。
        """
        f = [x for x in defn["factors"] if x["key"] == "cn_gold"][0]
        scores = {r[2] for r in defn["thresholds"][f["thresholds_key"]]}
        assert scores == {-1, 0, 1}

    def test_drive_full_matches_the_actual_achievable_max(self, defn):
        """drive_full 必须等于「各驱动力因子的最大得分之和」，不是「因子数 × 2」。"""
        st = defn["stages"]
        by_key = {f["key"]: f for f in defn["factors"]}
        real_max = 0
        for k in st["drive_keys"]:
            table = defn["thresholds"][by_key[k]["thresholds_key"]]
            real_max += max(r[2] for r in table)
        assert st["drive_full"] == real_max == 11

    def test_position_is_not_in_the_drive_axis(self, defn):
        """位置维度必须**不在** drive_keys 里，否则两条轴的正交性就没了。"""
        st = defn["stages"]
        assert st["pos_key"] not in st["drive_keys"]
        assert st["pos_key"] in {f["key"] for f in defn["factors"]}

    def test_stage_cells_cover_all_nine_combinations(self, defn):
        st = defn["stages"]
        need = {(d["name"], p["name"])
                for d in st["drive_bands"] for p in st["pos_bands"]}
        got = {(c["drive"], c["pos"]) for c in st["cells"]}
        assert got == need, need - got
        # 阶段名必须唯一，否则页面上无法定位「当前格」
        names = [c["stage"] for c in st["cells"]]
        assert len(names) == len(set(names)) == 9


# ---------------------------------------------------------------------------
# pct_change_window
# ---------------------------------------------------------------------------

_PCT_FACTOR = [{
    "key": "x", "compute": "pct_change_window", "name": "变动率",
    "series": "s", "metric": "x_chg", "raw_metric": "x_raw",
    "unit": "%", "raw_unit": "吨", "label": "库存",
    "window_label": "个交易日", "lookback_periods": 20,
    "thresholds_key": "t", "source": "测试",
}]

_PCT_TH = {"t": [[-9999, -3, -2], [-3, -1, -1], [-1, 1, 0], [1, 3, 1], [3, 9999, 2]]}


class TestPctChangeWindow:
    def _engine(self, values):
        return StubEngine({"X": _daily(values)})

    def _defn(self):
        return _defn(_PCT_FACTOR, {"s": {"kind": "macro", "code": "X"}}, _PCT_TH)

    def test_positive_change_scores_up(self):
        vals = [100.0] * 20 + [101.72]
        r = _run(self._engine(vals), self._defn())
        f = r["factors"][0]
        assert f["value"] == pytest.approx(1.72, abs=0.01)
        assert f["score"] == 1
        assert f["unit"] == "%"

    def test_negative_change_scores_down(self):
        vals = [100.0] * 20 + [96.5]
        f = _run(self._engine(vals), self._defn())["factors"][0]
        assert f["value"] == pytest.approx(-3.5, abs=0.01)
        assert f["score"] == -2

    def test_short_history_is_missing_not_neutral(self):
        """历史不足必须是 score=None（数据缺失），绝不能给 0 分当「中性」。"""
        f = _run(self._engine([100.0] * 5), self._defn())["factors"][0]
        assert f["value"] is None and f["score"] is None
        assert "历史不足" in f["detail"]

    def test_zero_base_is_missing(self):
        vals = [0.0] * 20 + [10.0]
        f = _run(self._engine(vals), self._defn())["factors"][0]
        assert f["score"] is None and "基期读数无效" in f["detail"]


# ---------------------------------------------------------------------------
# ratio_pct_change —— 必须按共同交易日对齐
# ---------------------------------------------------------------------------

_RATIO_FACTOR = [{
    "key": "r", "compute": "ratio_pct_change", "name": "⑤ 金银比",
    "a": "au", "b": "ag", "scale_a": 1000, "scale_b": 1,
    "metric": "r_chg", "raw_metric": "r_raw",
    "unit": "%", "label": "金银比", "ratio_unit": "",
    "window_label": "个交易日", "lookback_periods": 20,
    "thresholds_key": "t", "source": "测试",
}]

_RATIO_TH = {"t": [[-9999, -5, 2], [-5, -1, 1], [-1, 1, 0], [1, 5, -1], [5, 9999, -2]]}


class TestRatioPctChange:
    def _defn(self):
        return _defn(_RATIO_FACTOR,
                     {"au": {"kind": "macro", "code": "AU"},
                      "ag": {"kind": "macro", "code": "AG"}},
                     _RATIO_TH)

    def test_ratio_and_alignment_on_common_dates(self):
        """金比银多两个交易日时必须按**共同日**配对，不能用各自的最后一行。

        夹具里金在 09-23/09-24 大幅跳涨（900 → 990），银只到 09-22（16000）。
        若错用「各取最新一行」，比值 = 990*1000/16000 = 61.9（虚假跳变）；
        按共同日配对则末端是 09-22 的 900*1000/16000 = 56.25。
        """
        n = 25
        base = date(2026, 9, 22)
        au_rows = [{"report_date": (base - timedelta(days=n - 1 - i)).isoformat(),
                    "value": 800.0 + i * 5} for i in range(n)]
        au_rows += [{"report_date": "2026-09-23", "value": 900.0},
                    {"report_date": "2026-09-24", "value": 990.0}]
        ag_rows = [{"report_date": (base - timedelta(days=n - 1 - i)).isoformat(),
                    "value": 16000.0} for i in range(n)]
        r = _run(StubEngine({"AU": au_rows, "AG": ag_rows}), self._defn())
        f = r["factors"][0]
        # 共同日末端 = 09-22：金 800+24*5 = 920 → 920*1000/16000 = 57.5
        assert f["asof"] == "2026-09-22"
        assert r["metric_values"]["r_raw"] == pytest.approx(57.5, abs=0.01)

    def test_falling_ratio_is_favourable(self):
        """金银比回落 = 白银更强 = 贵金属整体偏强 → +2。"""
        n = 25
        au = [{"report_date": (date(2026, 9, 22) - timedelta(days=n - 1 - i)).isoformat(),
               "value": 900.0} for i in range(n)]
        # 白银持续走强（价格上升 8%）→ 比值下降约 7.4%
        ag = [{"report_date": row["report_date"], "value": 15800.0 * (1.08 ** (i / (n - 1)))}
              for i, row in enumerate(au)]
        f = _run(StubEngine({"AU": au, "AG": ag}), self._defn())["factors"][0]
        assert f["value"] < -5
        assert f["score"] == 2

    def test_insufficient_overlap_is_missing(self):
        au = _daily([900.0] * 5, end=date(2026, 9, 22))
        ag = _daily([16000.0] * 5, end=date(2026, 8, 1))
        f = _run(StubEngine({"AU": au, "AG": ag}), self._defn())["factors"][0]
        assert f["score"] is None and "共同交易日不足" in f["detail"]


# ---------------------------------------------------------------------------
# spread_level_chg —— 日频 − 月频必须按月对齐
# ---------------------------------------------------------------------------

_SPREAD_FACTOR = [{
    "key": "real", "compute": "spread_level_chg", "name": "③ 实际利率（代理）",
    "a": "us10", "b": "cpi", "metric": "real_chg", "raw_metric": "real_level",
    "unit": "pp", "label_a": "美债10Y名义", "label_b": "美国CPI同比",
    "thresholds_key": "t", "source": "测试",
}]

_SPREAD_TH = {"t": [[-9999, -0.2, 2], [-0.2, -0.05, 1], [-0.05, 0.05, 0],
                    [0.05, 0.2, -1], [0.2, 9999, -2]]}


class TestSpreadLevelChg:
    def _defn(self):
        return _defn(_SPREAD_FACTOR,
                     {"us10": {"kind": "macro", "code": "US10"},
                      "cpi": {"kind": "macro", "code": "CPI"}},
                     _SPREAD_TH)

    def _us10(self):
        """6/7/8 三个月，每月最后一个交易日的 10Y：4.50 / 4.80 / 4.75。"""
        rows = []
        for ym, last in ((M3, 4.50), (M2, 4.80), (M1, 4.75)):
            y, m = int(ym[:4]), int(ym[5:7])
            for d in range(1, 29):
                rows.append({"report_date": "%s-%02d" % (ym, d), "value": 4.0})
            # 覆盖该月最后一个记录（升序遍历时最后写入者胜出）
            rows.append({"report_date": "%s-%02d" % (ym, 28), "value": last})
        return sorted(rows, key=lambda r: r["report_date"])

    def test_month_alignment_uses_last_reading_of_each_month(self):
        """CPI 只有月频且最新一期是 8 月：绝不能拿 9 月的国债去减 8 月的 CPI。"""
        cpi = _monthly([(M3, "2026-06-30", 3.20), (M2, "2026-07-31", 3.40),
                        (M1, "2026-08-31", 3.40)])
        f = _run(StubEngine({"US10": self._us10(), "CPI": cpi}),
                 self._defn())["factors"][0]
        # 7 月：4.80 − 3.40 = 1.40；8 月：4.75 − 3.40 = 1.35 → 环比 −0.05
        assert f["asof"] == "2026-08-31"      # 取月频腿的完整日期，不是 "2026-08"
        assert f["value"] == pytest.approx(-0.05, abs=1e-9)
        # −0.05 落在 [-0.05, 0.05) 这一档（下限含、上限不含）→ 中性 0 分。
        # 与原始报告一致：报告里 ③ 的环比 +0.000pp 也是 0 分。
        assert f["score"] == 0
        assert "1.35" in f["detail"]

    def test_asof_is_a_full_date_not_a_year_month(self):
        """asof 必须是 YYYY-MM-DD：给 "YYYY-MM" 会让 `_is_stale` 解析失败 → 恒定误报陈旧。"""
        cpi = _monthly([(M3, "2026-06-30", 3.20), (M2, "2026-07-31", 3.40),
                        (M1, "2026-08-31", 3.40)])
        f = _run(StubEngine({"US10": self._us10(), "CPI": cpi}),
                 self._defn())["factors"][0]
        assert len(f["asof"]) == 10
        assert not f["stale"]

    def test_rising_spread_is_unfavourable(self):
        cpi = _monthly([(M3, "2026-06-30", 3.20), (M2, "2026-07-31", 3.40),
                        (M1, "2026-08-31", 3.00)])
        f = _run(StubEngine({"US10": self._us10(), "CPI": cpi}),
                 self._defn())["factors"][0]
        # 7 月 1.40 → 8 月 1.75，环比 +0.35 → -2
        assert f["value"] == pytest.approx(0.35, abs=1e-9)
        assert f["score"] == -2

    def test_single_common_month_is_missing(self):
        cpi = _monthly([(M1, "2026-08-31", 3.40)])
        f = _run(StubEngine({"US10": self._us10(), "CPI": cpi}),
                 self._defn())["factors"][0]
        assert f["score"] is None and "共同月份不足" in f["detail"]


# ---------------------------------------------------------------------------
# extension_rank —— 位置维度
# ---------------------------------------------------------------------------

_EXT_FACTOR = [{
    "key": "position", "compute": "extension_rank", "name": "⑦ 价格位置",
    "series": "au", "metric": "position_rank", "raw_metric": "position_ext",
    "ma_days": 20, "min_samples": 10, "label": "上海金",
    "unit": "%分位", "no_series_rank": True,
    "thresholds_key": "t", "source": "测试",
}]

_EXT_TH = {"t": [[-99, 10, 2], [10, 30, 1], [30, 70, 0], [70, 90, -1], [90, 999, -2]]}


class TestExtensionRank:
    def _defn(self):
        return _defn(_EXT_FACTOR, {"au": {"kind": "macro", "code": "AU"}}, _EXT_TH)

    def test_value_is_a_percentile_and_rank_metric_is_off(self):
        """读数就是分位（0~100），所以引擎不该再补一个「读数的分位」。"""
        vals = [100.0 + i for i in range(60)]
        r = _run(StubEngine({"AU": _daily(vals)}), self._defn())
        f = r["factors"][0]
        assert 0 <= f["value"] <= 100
        assert f["pct_rank"] is None and f["pct_rank_off"] is True

    def test_new_high_lands_in_the_top_band(self):
        """先平后急涨 → 末端延伸度是历史最大 → 分位 ~100% → 高位 = -2。

        注意**不能**用「匀速上涨」的夹具：线性上涨时相对延伸度在起点最大
        （分母最小），末端反而是低分位。夹具选错会让人误判因子方向反了。
        """
        vals = [100.0] * 50 + [100.0 + 3.0 * i for i in range(1, 11)]
        f = _run(StubEngine({"AU": _daily(vals)}), self._defn())["factors"][0]
        assert f["value"] > 90
        assert f["score"] == -2

    def test_insufficient_history_is_missing(self):
        f = _run(StubEngine({"AU": _daily([100.0] * 15)}), self._defn())["factors"][0]
        assert f["score"] is None and "历史不足" in f["detail"]


# ---------------------------------------------------------------------------
# 阶段矩阵（stages）
# ---------------------------------------------------------------------------

def _stages_cfg():
    return {
        "drive_keys": ["a", "b"],
        "pos_key": "p",
        "drive_full": 4,
        "pos_full": 2,
        "drive_bands": [{"name": "顺风", "min": 4}, {"name": "中性", "min": -3},
                        {"name": "逆风", "min": -99}],
        "pos_bands": [{"name": "低位", "min": 1}, {"name": "中性位", "min": -1},
                      {"name": "高位", "min": -99}],
        "cells": [
            {"drive": "顺风", "pos": "低位", "stage": "低位积累期", "tone": "bull"},
            {"drive": "顺风", "pos": "中性位", "stage": "顺风上行期", "tone": "bull"},
            {"drive": "顺风", "pos": "高位", "stage": "趋势延伸期", "tone": "warn"},
            {"drive": "中性", "pos": "低位", "stage": "低位磨底期", "tone": "warn"},
            {"drive": "中性", "pos": "中性位", "stage": "中性震荡期", "tone": "neutral"},
            {"drive": "中性", "pos": "高位", "stage": "高位僵持期", "tone": "neutral"},
            {"drive": "逆风", "pos": "低位", "stage": "逆风错杀期", "tone": "neutral"},
            {"drive": "逆风", "pos": "中性位", "stage": "逆风调整期", "tone": "bear"},
            {"drive": "逆风", "pos": "高位", "stage": "高位风险期", "tone": "bear"},
        ],
    }


def _mk_factors(drive_scores, pos_score):
    out = []
    for i, s in enumerate(drive_scores):
        out.append({"key": "ab"[i], "name": "d%d" % i, "value": 1.0, "unit": "",
                    "score": s, "status": "x", "detail": "", "source": "", "asof": "2026-09-24"})
    out.append({"key": "p", "name": "pos", "value": 50.0, "unit": "%分位",
                "score": pos_score, "status": "x", "detail": "", "source": "", "asof": "2026-09-24"})
    return out


class TestStageMatrix:
    @pytest.mark.parametrize("drive,pos,expect", [
        (4, 2, "低位积累期"),      # 顺风 + 低位
        (4, 0, "顺风上行期"),
        (4, -2, "趋势延伸期"),
        (0, 2, "低位磨底期"),
        (0, 0, "中性震荡期"),
        (0, -2, "高位僵持期"),
        (-4, 2, "逆风错杀期"),
        (-4, 0, "逆风调整期"),
        (-5, -2, "高位风险期"),
    ])
    def test_nine_cells(self, drive, pos, expect):
        st = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([drive // 2, drive - drive // 2], pos), [])
        assert st["stage"] == expect
        assert sum(1 for c in st["cells"] if c["current"]) == 1

    def test_band_boundaries(self):
        """档位边界：驱动 4/−3、位置 1/−1（下限含）。"""
        cases = [([2, 2], "顺风"), ([2, -5], "中性"), ([-2, -1], "中性"), ([-2, -2], "逆风")]
        for scores, expect in cases:
            st = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors(scores, 0), [])
            assert st["drive_band"] == expect, scores
        for score, expect in [(2, "低位"), (1, "低位"), (0, "中性位"), (-1, "中性位"), (-2, "高位")]:
            st = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([0, 0], score), [])
            assert st["pos_band"] == expect, score

    def test_position_is_excluded_from_the_drive_axis(self):
        """位置得分变化不得影响驱动力轴——两条轴正交是这个功能的前提。"""
        w = []
        a = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([2, 1], 2), w)
        b = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([2, 1], -2), w)
        assert a["drive_total"] == b["drive_total"] == 3
        assert a["stage"] != b["stage"]

    def test_missing_drive_factor_is_flagged_but_still_judged(self):
        """部分缺失照常给档（与 total 口径一致），但必须显式警告。"""
        factors = _mk_factors([2, None], 0)
        w = []
        st = ThesisEngine._eval_stages(_stages_cfg(), factors, w)
        assert st["complete"] is False
        assert st["drive_missing"] == ["b"]
        assert st["drive_total"] == 2
        assert st["stage"] == "中性震荡期"
        assert any("只累加" in x for x in w)

    def test_all_drive_missing_gives_no_stage(self):
        w = []
        st = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([None, None], 0), w)
        assert st["drive_total"] is None and st["stage"] is None
        assert any("无法判定" in x for x in w)

    def test_pos_keys_sums_multiple_factors(self):
        """pos_keys（多项求和）为猪周期 B 轴而加：④+⑤+⑥ 共同构成位置维度。

        只取单项会把 B 轴满分从 ±5 缩到 ±2、档位整体失真，故这里锁死「求和」语义。
        """
        cfg = dict(_stages_cfg())
        cfg.pop("pos_key", None)
        cfg["pos_keys"] = ["p1", "p2", "p3"]
        cfg["pos_full"] = 6
        factors = [{"key": "a", "score": 2, "value": 1, "name": "a", "unit": ""},
                   {"key": "b", "score": 1, "value": 1, "name": "b", "unit": ""},
                   {"key": "p1", "score": -2, "value": 1, "name": "p1", "unit": ""},
                   {"key": "p2", "score": 0, "value": 1, "name": "p2", "unit": ""},
                   {"key": "p3", "score": -1, "value": 1, "name": "p3", "unit": ""}]
        st = ThesisEngine._eval_stages(cfg, factors, [])
        assert st["pos_total"] == -3                 # −2 + 0 + −1
        assert st["pos_keys"] == ["p1", "p2", "p3"]
        assert st["pos_full"] == 6
        assert st["complete"] is True

    def test_pos_keys_missing_one_still_judged_but_flagged(self):
        """pos_keys 中缺一项：照常按剩余相加给档，但必须显式警告。"""
        cfg = dict(_stages_cfg())
        cfg.pop("pos_key", None)
        cfg["pos_keys"] = ["p1", "p2"]
        factors = [{"key": "a", "score": 2, "value": 1, "name": "a", "unit": ""},
                   {"key": "b", "score": 1, "value": 1, "name": "b", "unit": ""},
                   {"key": "p1", "score": 1, "value": 1, "name": "p1", "unit": ""},
                   {"key": "p2", "score": None, "value": None, "name": "p2", "unit": ""}]
        w = []
        st = ThesisEngine._eval_stages(cfg, factors, w)
        assert st["pos_total"] == 1
        assert st["pos_missing"] == ["p2"]
        assert st["complete"] is False
        assert any("只累加" in x for x in w)

    def test_pos_key_singular_still_supported(self):
        """向后兼容：未声明 pos_keys 时仍按 pos_key 单因子走（黄金/铜/红利）。"""
        st = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([2, 1], 2), [])
        assert st["pos_keys"] == ["p"]
        assert st["pos_total"] == st["pos_score"] == 2

    def test_missing_position_gives_no_stage(self):
        w = []
        st = ThesisEngine._eval_stages(_stages_cfg(), _mk_factors([2, 2], None), w)
        assert st["pos_score"] is None and st["stage"] is None

    def test_empty_config_returns_none(self):
        """未声明 stages 的台账（红利/恒科/债基）不能因此报错或出现空块。"""
        assert ThesisEngine._eval_stages({}, _mk_factors([2, 2], 0), []) is None


# ---------------------------------------------------------------------------
# 全定义离线跑通：真实 gold_regime.json + 合成数据
# ---------------------------------------------------------------------------

def _gold_stub_series():
    """构造一份能跑通全部 7 因子的合成序列（含 1100 日金价以满足位置分位样本要求）。"""
    n = 1100
    base = date(2026, 9, 24)
    au_rows, ag_rows = [], []
    for i in range(n):
        d = (base - timedelta(days=n - 1 - i)).isoformat()
        # 轻微上行 + 周期波动，使末端延伸度落在分位中段
        au = 700.0 + i * 0.15 + 40.0 * ((i % 60) / 60.0)
        au_rows.append({"report_date": d, "value": round(au, 2)})
        # 金银比刻意做成有波动（57 ± 2），否则比值恒定 → 变动率恒为 0，
        # ⑤ 永远落在中性档，测不出「比值变化」这条链路是否真的通了。
        ag_rows.append({"report_date": d,
                        "value": round(au * 1000 / (57.0 + 2.0 * math.sin(i / 40.0)), 2)})

    dxy = _daily([96.5] * 20 + [98.0])
    us2 = _daily([4.19] * 20 + [4.87])
    us10 = []
    for ym, last in ((M3, 4.55), (M2, 4.85), (M1, 4.80)):
        for day in range(1, 29):
            us10.append({"report_date": "%s-%02d" % (ym, day), "value": 4.4})
        us10.append({"report_date": "%s-28" % ym, "value": last})
    us10.sort(key=lambda r: r["report_date"])
    cpi = _monthly([(M3, "2026-06-30", 3.20), (M2, "2026-07-31", 3.40),
                    (M1, "2026-08-31", 3.40)])
    etf = _daily([1038.9] * 20 + [1056.8])
    cn_gold = _monthly([("2026-03", "2026-03-31", 7370.0), ("2026-04", "2026-04-30", 7410.0),
                        ("2026-05", "2026-05-31", 7496.0), ("2026-06", "2026-06-30", 7530.0),
                        ("2026-07", "2026-07-31", 7600.0), (M1, "2026-08-31", 7673.0)])

    return {
        "dxy": dxy, "us2_y": us2, "us10_y": us10, "us_cpi_yoy": cpi,
        "gold_etf_holdings": etf, "cn_gold_reserve": cn_gold,
        "au_sge": au_rows, "ag_sge": ag_rows,
    }


class TestGoldFullDefinitionOffline:
    @pytest.fixture(scope="class")
    def result(self):
        # 走真实定义（data/thesis/gold_regime.json）+ 合成数据，完全离线
        return ThesisEngine.compute(StubEngine(_gold_stub_series()), "gold_regime")

    def test_all_seven_factors_valid(self, result):
        assert result["valid_count"] == result["factor_count"] == 7
        assert result["warnings"] == []

    def test_total_is_the_sum_of_factor_scores(self, result):
        assert result["total"] == sum(f["score"] for f in result["factors"])

    def test_stage_block_present_and_consistent(self, result):
        st = result["stages"]
        assert st is not None and st["stage"]
        assert st["drive_full"] == 11 and st["pos_full"] == 2
        assert st["drive_valid_count"] == st["drive_factor_count"] == 6
        assert st["complete"] is True
        # 驱动力只累加 6 个驱动力因子，不含位置
        drive_sum = sum(f["score"] for f in result["factors"]
                        if f["key"] in st["drive_keys"])
        assert st["drive_total"] == drive_sum
        assert st["pos_score"] == [f for f in result["factors"]
                                   if f["key"] == "position"][0]["score"]

    def test_expected_stage_from_the_synthetic_data(self, result):
        """合成数据：驱动 = -1-2+1+1+1+1 = ... 位置中段。

        这里只钉「阶段名与两条轴的档位自洽」，不钉具体数值——
        数值随合成数据微调而变，钉死会让无关改动频繁红。
        """
        st = result["stages"]
        name_by = {(c["drive"], c["pos"]): c["stage"] for c in st["cells"]}
        assert st["stage"] == name_by[(st["drive_band"], st["pos_band"])]

    def test_stages_do_not_enter_total(self, result):
        """阶段矩阵是研究表达层，不能反过来影响打分。

        total 只等于因子得分之和；stages 里出现的 drive_total / pos_score
        都不是额外加分项——否则「阶段」会与「总分」互相污染（总分里含位置，
        而位置又被用来决定阶段档位）。
        """
        st = result["stages"]
        assert result["total"] == sum(f["score"] for f in result["factors"]
                                      if f["score"] is not None)
        drive_sum = sum(f["score"] for f in result["factors"]
                        if f["key"] in st["drive_keys"])
        assert st["drive_total"] == drive_sum
        assert result["total"] == drive_sum + st["pos_score"]

    def test_signals_and_invalidation_reference_known_metrics(self, result):
        metrics = result["metric_values"]
        for group in ("buy", "warn"):
            for item in result["signals"][group]:
                assert item["label"]
                assert item["current"] is not None, item
        for rule in result["invalidation"]:
            assert rule["text"]
            assert rule["current"] is not None, rule
        # 关键原始读数都进了 metrics（信号与情景要靠它们判断）
        for k in ("dxy_chg_bp", "us2_chg_bp", "real_chg_pp", "etf_chg_pct",
                  "agratio_chg_pct", "cn_gold_chg_pct", "position_rank",
                  "agratio", "etf_level", "real_level"):
            assert k in metrics, k

    def test_scenarios_and_transmission_evaluated(self, result):
        assert len(result["scenarios"]) == 3
        assert len(result["transmission"]) == 4
        for s in result["scenarios"]:
            assert s["conditions"], s["name"]
        # phases 按数据口径日期定位当前阶段
        assert any(p["current"] for p in result["phases"])

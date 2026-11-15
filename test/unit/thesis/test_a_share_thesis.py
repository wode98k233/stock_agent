"""A股台账（a_share_regime）：frames 版位置因子 + 六因子合成，完全离线。

为什么单独一个文件：

1. **新增了 frames 版位置计算器 `frame_ma_rank`**。它与 series 版的 `vs_ma_rank`
   看着一样、读的数据通道完全不同：宽基指数在 `market_data_db.index_daily`（frames），
   而黄金/铜/猪的主标的在 macro 长表（series）。用错通道**不会报错**，只会静默
   退化成「历史不足」的空因子——位置维度永远空白，而总分看起来依然正常。
   这里把两条通道的差异以及「样本不足不许硬算」钉住。

2. **剪刀差方向与本系统另一张台账相反**。`dividend_env` ⑥ 是「越负越利好红利」
   （资金淤积 → 类固收占优），本台账 ③ 是「越正越利好宽基」（信用活化 → 权益普涨）。
   阈值符号照抄会得到完全相反的结论，且分数依然落在合法区间里、不会报错。

3. **阈值区间是左闭右开**：-4.0pp 落在 [-4, -1) 得 1 分而不是 0 分。
   这类边界在「微调档位」时最容易改错，而改错的后果只是分数差 1 分、无人察觉。
"""
import json
import pandas as pd
import pytest

from thesis import defs
from thesis.engine import ThesisEngine
from datetime import date, timedelta
from pathlib import Path

TODAY = date(2026, 10, 30)
ROOT = Path(__file__).resolve().parents[3]
A_DEF = ROOT / "data" / "thesis" / "a_share_regime.json"


def _series(values, end=None):
    """按自然日倒推生成日频序列（升序）。"""
    end = end or date(2026, 10, 29)
    n = len(values)
    return [{"report_date": (end - timedelta(days=n - 1 - i)).isoformat(), "value": float(v)}
            for i, v in enumerate(values)]


def _frame(closes, amounts=None):
    """构造 index_daily 形态的 DataFrame（engine 只用 trade_date/close/amount）。"""
    n = len(closes)
    start = date(2026, 10, 29) - timedelta(days=n - 1)
    return pd.DataFrame({
        "trade_date": [(start + timedelta(days=i)).isoformat() for i in range(n)],
        "close": [float(x) for x in closes],
        "amount": [float(x) for x in (amounts or [5.0e11] * n)],
    })


class StubEngine(ThesisEngine):
    """取数层换内存数据；未提供的 code 一律返回空，让对应因子走「数据缺失」分支。"""

    def __init__(self, series=None, frames=None, val_ok=True):
        super().__init__()
        self._series = series or {}
        self._frames = frames or {}
        self._val_ok = val_ok

    def today(self):
        return TODAY

    def macro_stale_threshold(self, code):
        # 月频指标按 45 天判陈旧，日频 10 天（真值来自 macro_db.stale_threshold_days）
        return 45 if str(code) in ("m1_yoy", "m2_yoy") else 10

    def macro_series(self, code, periods=60):
        return list(self._series.get(str(code), []))

    def sync_index_valuation(self, index_code):
        # 真实实现会打中证指数公司接口；这里只回 code 名，让 _prepare 去 macro_series 取
        return {"dy_code": "idx_000906_dy", "pe_code": "idx_000906_pe",
                "ok": self._val_ok}

    def _load_index(self, code, days, name, warnings, label, params):
        return self._frames.get(str(code))


def _full_series():
    """默认数据：汇率 6.90、利率 1.80、剪刀差 -4.0pp、盈利收益率 7.5%（ERP=5.7pp）。

    v1.2 起 ② 用 E/P（idx800_ey）而非股息率（idx_000906_dy）——后者源只回 20 个交易日，
    长历史无从建立，见 ledger note。
    """
    return {
        "usdcny": _series([6.90] * 30),
        "cn10_y": _series([1.80] * 30),
        "m1_yoy": _series([3.2] * 12),
        "m2_yoy": _series([7.2] * 12),
        "idx800_ey": _series([7.5] * 100),
    }


def _flat_then_drop():
    """550 天 5000 点 + 50 天 4200 点 → 相对 MA250 约 -13%。"""
    return _frame([5000.0] * 550 + [4200.0] * 50)


# ---------------------------------------------------------------------------
# 定义本身的守卫
# ---------------------------------------------------------------------------

class TestDefinition:
    @pytest.fixture(scope="class")
    def defn(self):
        return json.loads(A_DEF.read_text(encoding="utf-8"))

    def test_loads_and_validates(self):
        d, problems = defs.load_def_checked("a_share_regime")
        assert d is not None
        assert problems == [], problems

    def test_listed(self):
        assert "a_share_regime" in defs.list_defs()

    def test_four_factors(self, defn):
        """v1.4 起为 4 因子：汇率（方向不稳）与成交拥挤度（与位置相关 0.80）已剔除。

        剔除依据见定义里的 `_说明_刻意排除`：6 因子组合 r=0.210 → 4 因子 r=0.569（60 日，
        首次显著），q5q1 从 10.8pp 升到 18.0pp。此断言防止有人"补回完整性"而把组合打回原形。
        """
        assert len(defn["factors"]) == 4
        keys = {f["key"] for f in defn["factors"]}
        assert keys == {"premium", "credit", "rate", "position"}
        assert "fx" not in keys and "crowding" not in keys

    def test_every_compute_is_registered(self, defn):
        for f in defn["factors"]:
            assert f["compute"] in ThesisEngine.FACTOR_COMPUTERS, f["compute"]

    def test_every_threshold_key_exists(self, defn):
        for f in defn["factors"]:
            if f.get("thresholds_key"):
                assert f["thresholds_key"] in defn["thresholds"], f["key"]

    def test_threshold_tables_cover_whole_range(self, defn):
        """没兜住 -99~99 时越界值会静默变「数据缺失」。"""
        for name, table in defn["thresholds"].items():
            assert min(r[0] for r in table) <= -99, name
            assert max(r[1] for r in table) >= 99, name

    def test_verdicts_cover_min_total(self, defn):
        mins = [v["min_total"] for v in defn["verdicts"]]
        assert min(mins) <= -99
        assert mins == sorted(mins, reverse=True)

    def test_score_tables_within_pm2(self, defn):
        for name, table in defn["thresholds"].items():
            for lo, hi, sc in table:
                assert -2 <= sc <= 2, (name, sc)


# ---------------------------------------------------------------------------
# frames 版位置因子：通道与降级
# ---------------------------------------------------------------------------

class TestFrameMaRank:
    def _spec(self, **kw):
        base = {"key": "position", "compute": "frame_ma_rank", "name": "⑥ 位置",
                "frame": "idx800", "metric": "ma_gap", "raw_metric": "ma250",
                "ma_days": 250, "min_samples": 250, "unit": "%",
                "thresholds_key": "ma_gap_pct"}
        base.update(kw)
        return base

    def _defn(self, table=None):
        return {
            "id": "t_a", "name": "t", "version": "1.0",
            "subject": {"index_code": "000906", "benchmark_code": "000300"},
            "data": {"idx800": {"kind": "index_daily", "code": "000906", "days": 600}},
            "factors": [self._spec()],
            "params": {"index_stale_days": 5, "min_samples_for_position_rank": 250},
            "thresholds": {"ma_gap_pct": table or [
                [-99, -10, 2], [-10, -4, 1], [-4, 4, 0], [4, 10, -1], [10, 99, -2]]},
            "verdicts": [{"min_total": -99, "verdict": "x", "action": "y"}],
        }

    def test_reads_frames_not_series(self):
        """同样的收盘价放在 series（macro）里，本计算器必须取不到——通道不通用。"""
        eng = StubEngine(series={"idx800": _series([5000.0] * 600)})
        res = eng.compute("t_a", defn=self._defn())
        assert res["factors"][0]["value"] is None
        assert "无日线数据" in res["factors"][0]["detail"]

    def test_gap_is_computed_from_frames(self):
        eng = StubEngine(frames={"000906": _flat_then_drop()})
        res = eng.compute("t_a", defn=self._defn())
        val = res["factors"][0]["value"]
        # MA250 = (5000*200 + 4200*50)/250 = 4840 → 4200/4840-1 = -13.22%
        assert val == pytest.approx(-13.22, abs=0.05)

    def test_deep_drawdown_scores_positive(self):
        """均值回归口径：偏离越深给分越高（与趋势跟随相反）。"""
        eng = StubEngine(frames={"000906": _flat_then_drop()})
        res = eng.compute("t_a", defn=self._defn())
        assert res["factors"][0]["score"] == 2

    def test_insufficient_history_is_missing_not_guessed(self):
        """500 期是下限（250 均线 + 250 分位样本）；499 期必须判缺失而不是硬算。"""
        eng = StubEngine(frames={"000906": _frame([5000.0] * 499)})
        res = eng.compute("t_a", defn=self._defn())
        assert res["factors"][0]["value"] is None
        assert "历史不足" in res["factors"][0]["detail"]

    def test_empty_frame_is_missing(self):
        eng = StubEngine(frames={"000906": pd.DataFrame({"trade_date": [], "close": []})})
        res = eng.compute("t_a", defn=self._defn())
        assert res["factors"][0]["value"] is None


# ---------------------------------------------------------------------------
# 端到端：六因子合成
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def _run(self, **kw):
        eng = StubEngine(series=_full_series(), frames={"000906": _flat_then_drop()}, **kw)
        return eng.compute("a_share_regime")

    def test_all_factors_have_values(self):
        r = self._run()
        missing = [f["name"] for f in r["factors"] if f["value"] is None]
        assert missing == [], "有因子取不到值: %s" % missing

    def test_total_equals_sum_of_scores(self):
        r = self._run()
        valid = [f for f in r["factors"] if f["score"] is not None]
        assert r["total"] == sum(f["score"] for f in valid)
        assert r["verdict"] in {v["verdict"] for v in
                                json.loads(A_DEF.read_text(encoding="utf-8"))["verdicts"]}

    def test_stale_valuation_degrades_gracefully(self):
        """估值序列缺失时，只应让 ② 缺失，其余五个因子照常（单源失败不阻断）。"""
        series = _full_series()
        series.pop("idx800_ey")
        eng = StubEngine(series=series, frames={"000906": _flat_then_drop()}, val_ok=False)
        r = eng.compute("a_share_regime")
        by_key = {f["key"]: f for f in r["factors"]}
        assert by_key["premium"]["value"] is None
        for k in ("credit", "rate", "position"):
            assert by_key[k]["value"] is not None, k


# ---------------------------------------------------------------------------
# 方向与边界：两张台账最容易混淆的两处
# ---------------------------------------------------------------------------

class TestDirectionsAndBounds:
    def test_m1m2_direction_matches_dividend_env(self):
        """两张台账的剪刀差方向**一致**（越负越有利）。

        曾一度写成相反——当时的推理是「信用活化利好宽基、资金淤积利好红利」。
        2026-11-01 审计推翻了它：2012-2026 中证800 未来 12 个月收益，剪刀差最负 20% 档
        +10.0%（胜率 70.6%）、最正 20% 档 −0.9%（胜率 54.5%），方向与红利口径同向。
        真正起作用的是「剩余流动性」而非「信用活化」。此断言防止有人再按直觉改回去。
        """
        a = json.loads(A_DEF.read_text(encoding="utf-8"))
        div = json.loads((ROOT / "data" / "thesis" / "dividend_env.json")
                         .read_text(encoding="utf-8"))
        ta = a["thresholds"]["m1m2_gap_pp"]
        td = div["thresholds"]["m1m2_gap_pp"]
        # 同取最负的一档，得分必须同为「最有利」
        assert ta[0][2] == 2 and td[0][2] == 2

    @pytest.mark.parametrize("value,expect", [
        # v1.3 起边界来自实测分位（p20=-7.62 / p40=-5.30 / p60=-3.40 / p80=+1.58）
        (-7.62, 1),   # 左闭：落在 [-7.62,-5.30) → +1
        (-7.63, 2),   # 落入 [-99,-7.62) → +2
        (-5.30, 0),
        (-3.40, -1),  # 左闭：[-5.30,-3.40) 的上界不含，落入 [-3.40,1.58) → -1
        (1.58, -2),   # 左闭：落在 [1.58,99) → -2
        (-3.39, -1),
    ])
    def test_gap_bucket_is_left_closed(self, value, expect):
        from thesis import defs as d
        table = json.loads(A_DEF.read_text(encoding="utf-8"))["thresholds"]["m1m2_gap_pp"]
        assert d.score_by_table(value, table) == expect


"""恒生科技台账：`dual_axis` 因子计算器 + 定义校验，完全离线。

为什么单独一个文件：`dual_axis` 与 `double_tightening` 是**语义相反**的两个判定器
（前者两轴对称、后者以中债为主轴），最容易出的错就是「照抄债基的判定表」。
这里把两个判定器在同一组输入下的差异显式钉住，防止将来有人「顺手统一」。
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from thesis import defs
from thesis.engine import ThesisEngine

TODAY = date(2026, 9, 18)
END = date(2026, 9, 17)
ROOT = Path(__file__).resolve().parents[3]
SENG_DEF = ROOT / "data" / "thesis" / "hang_seng_tech.json"


def _series(values, end=END):
    n = len(values)
    return [{"report_date": (end - timedelta(days=n - 1 - i)).isoformat(), "value": v}
            for i, v in enumerate(values)]


def _index_df(closes, end=END):
    n = len(closes)
    return pd.DataFrame({
        "code": ["X"] * n,
        "trade_date": [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)],
        "close": [float(c) for c in closes],
    })


_MATRIX = {
    "num_up_denom_down": 2,
    "num_up_denom_up": 0,
    "num_down_denom_down": 0,
    "num_down_denom_up": -2,
    "num_flat_denom_up": -1,
    "num_flat_denom_down": 1,
    "num_up_denom_flat": 1,
    "num_down_denom_flat": -1,
    "num_flat_denom_flat": 0,
}


def _dual_defn(matrix=None):
    """最小 dual_axis 定义，便于逐项验算。"""
    return {
        "id": "t_dual", "name": "双轴测试", "version": "1.0",
        "subject": {"index_code": "HSTECH", "benchmark_code": "HSI"},
        # data 必须声明，否则 _prepare 不会往 ctx 里放序列 → 因子读到空 → 误判「数据缺失」
        "data": {
            "cn10_y": {"kind": "macro", "code": "cn10_y"},
            "us10_y": {"kind": "macro", "code": "us10_y"},
        },
        "factors": [
            {"key": "dual_axis", "compute": "dual_axis", "name": "② 内外双轴",
             "num": "cn10_y", "denom": "us10_y", "matrix": "dual_axis",
             "metric": "dual_axis", "num_metric": "cn10_y", "denom_metric": "us10_y",
             "num_chg_metric": "cn10_chg_bp", "denom_chg_metric": "us10_chg_bp",
             "lookback_days": 20, "move_bp": 10, "unit": "档", "source": "测试来源"},
        ],
        "matrices": {"dual_axis": dict(matrix if matrix is not None else _MATRIX)},
        "thresholds": {"unused": [[-99, 99, 0]]},
        "verdicts": [{"min_total": -99, "verdict": "x", "action": "y"}],
    }


class StubEngine(ThesisEngine):
    """取数层换内存数据：cn10 / us10 两组序列。"""

    def __init__(self, *, cn10=None, us10=None):
        super().__init__()
        self._cn10 = cn10 or []
        self._us10 = us10 or []

    def today(self):
        return TODAY

    def macro_stale_threshold(self, code):
        return 10

    def macro_series(self, code, periods=60):
        return (self._us10 if str(code) == "us10_y" else self._cn10)[-periods:]

    def read_index_daily(self, code, days):
        return pd.DataFrame()

    def collect_index_daily(self, code, days, name=""):
        return 0


def _run(engine, defn):
    return engine.compute(defn["id"], defn=defn)


def _only(res):
    return res["factors"][0]


class TestDualAxisMatrix:
    """3×3 方向组合。中国利率**上行**为分子端有利方向，与债基/红利相反。"""

    @pytest.mark.parametrize("cn_chg,us_chg,expect_key,expect_score", [
        # 中国上行 + 美债下行 → 双利好
        (+30, -30, "num_up_denom_down", 2),
        # 中国复苏 + 美债上行 → 拉锯
        (+30, +30, "num_up_denom_up", 0),
        # 中国走弱 + 美债下行 → 拉锯
        (-30, -30, "num_down_denom_down", 0),
        # 双杀
        (-30, +30, "num_down_denom_up", -2),
        # 单轴 flat：另一轴照常计分、强度减半（不归零）
        (+0.3, +30, "num_flat_denom_up", -1),
        (+0.3, -30, "num_flat_denom_down", 1),
        (+30, +0.3, "num_up_denom_flat", 1),
        (-30, +0.3, "num_down_denom_flat", -1),
        # 两轴都不动 → 无信号
        (+0.3, +0.3, "num_flat_denom_flat", 0),
    ])
    def test_nine_direction_combinations(self, cn_chg, us_chg, expect_key, expect_score):
        # 构造 20 日前基线 + 当期：变化量由 (当期-基线)*100 bp 决定
        base_cn, now_cn = 1.70, 1.70 + cn_chg / 100.0
        base_us, now_us = 4.50, 4.50 + us_chg / 100.0
        cn = _series([base_cn] + [now_cn] * 20)
        us = _series([base_us] + [now_us] * 20)
        res = _run(StubEngine(cn10=cn, us10=us), _dual_defn())
        f = _only(res)
        assert f["value"] == expect_score, f"key={expect_key} 期望 {expect_score}，实际 {f['value']}"
        assert f["score"] == expect_score
        assert res["metric_values"]["dual_axis"] == expect_score
        assert res["metric_values"]["cn10_chg_bp"] == pytest.approx(cn_chg, abs=0.2)
        assert res["metric_values"]["us10_chg_bp"] == pytest.approx(us_chg, abs=0.2)
        assert not res["warnings"], res["warnings"]

    def test_move_threshold_is_respected(self):
        """变化量在 move_bp 之内视为「未动」（用 <= / >= 判定，边界含）。"""
        # 恰好等于阈值 → 视为动了
        cn = _series([1.70] + [1.80] * 20)      # +10bp == move
        us = _series([4.50] * 21)               # 0bp → flat
        res = _run(StubEngine(cn10=cn, us10=us), _dual_defn())
        assert _only(res)["value"] == 1          # num_up_denom_flat

    def test_custom_move_bp_from_spec_overrides_params(self):
        d = _dual_defn()
        d["factors"][0]["move_bp"] = 100
        cn = _series([1.70] + [1.80] * 20)       # +10bp < 100bp → flat
        us = _series([4.50] * 21)
        res = _run(StubEngine(cn10=cn, us10=us), d)
        assert _only(res)["value"] == 0          # num_flat_denom_flat

    def test_missing_matrix_key_is_neutral_and_warned(self):
        """矩阵缺键必须按中性处理并写进 warnings，且**不能**照常输出「双杀/双利好」——
        人话解释与评分自相矛盾比没解释更糟。"""
        partial = {k: v for k, v in _MATRIX.items() if k != "num_down_denom_up"}
        cn = _series([1.70] + [1.40] * 20)       # -30bp
        us = _series([4.50] + [4.80] * 20)       # +30bp
        res = _run(StubEngine(cn10=cn, us10=us), _dual_defn(matrix=partial))
        assert _only(res)["value"] == 0
        assert any("num_down_denom_up" in w for w in res["warnings"]), res["warnings"]
        detail = _only(res)["detail"]
        assert "未在 matrices" in detail and "按中性处理" in detail
        assert "双杀" not in detail, "缺键时不能仍输出带方向的结论"

    def test_detail_names_both_axes_in_bp(self):
        cn = _series([1.70] + [1.40] * 20)
        us = _series([4.50] + [4.80] * 20)
        res = _run(StubEngine(cn10=cn, us10=us), _dual_defn())
        detail = _only(res)["detail"]
        assert "中国 10Y" in detail and "美债 10Y" in detail
        assert "双杀" in detail


class TestDualAxisDataGaps:
    """「数据缺失 ≠ 中性」在双轴因子上的落实。"""

    def test_missing_num_series_is_none_not_zero(self):
        us = _series([4.50] + [4.80] * 20)
        res = _run(StubEngine(cn10=[], us10=us), _dual_defn())
        f = _only(res)
        assert f["score"] is None
        assert "数据缺失" in f["detail"]

    def test_missing_denom_series_is_none(self):
        cn = _series([1.70] + [1.80] * 20)
        res = _run(StubEngine(cn10=cn, us10=[]), _dual_defn())
        assert _only(res)["score"] is None

    def test_insufficient_history_is_none(self):
        """不足 n+1 期 → 数据缺失（不能拿不足的窗口硬算）。"""
        cn = _series([1.70, 1.80])      # 仅 2 期 < 21
        us = _series([4.50] + [4.80] * 20)
        res = _run(StubEngine(cn10=cn, us10=us), _dual_defn())
        assert _only(res)["score"] is None
        assert "历史不足" in _only(res)["detail"]

    def test_stale_when_either_axis_is_stale(self):
        cn = _series([1.70] + [1.80] * 20)
        us = _series([4.50] + [4.80] * 20)
        eng = StubEngine(cn10=cn, us10=us)
        # 中国轴陈旧、美债轴新鲜 → 整体标 stale（OR 语义）
        eng.macro_series = lambda code, periods=60: (
            [{"report_date": "2020-01-01", "value": 1.80}] * 21
            if str(code) == "cn10_y" else [{"report_date": END.isoformat(), "value": 4.80}] * 21)
        res = _run(eng, _dual_defn())
        assert _only(res)["stale"] is True


class TestRegressionAgainstDoubleTightening:
    """钉住两个判定器的语义差异——防止将来被「顺手统一」。"""

    def _both_defs(self):
        """同一组输入分别喂给 dual_axis 与 double_tightening。"""
        dual = _dual_defn()
        bond = {
            "id": "t_bond", "name": "债基对照", "version": "1.0",
            "subject": {"index_code": "H11001", "benchmark_code": "H11001"},
            "data": {
                "cn10_y": {"kind": "macro", "code": "cn10_y"},
                "us10_y": {"kind": "macro", "code": "us10_y"},
            },
            "factors": [
                {"key": "double_tighten", "compute": "double_tightening",
                 "name": "② 内外双紧", "cn": "cn10_y", "us": "us10_y",
                 "matrix": "double_tighten", "metric": "double_tighten",
                 "cn_metric": "cn10_y", "us_metric": "us10_y",
                 "cn_chg_metric": "cn10_chg_bp", "us_chg_metric": "us10_chg_bp",
                 "lookback_days": 20, "move_bp": 10, "unit": "档"},
            ],
            "matrices": {"double_tighten": {
                "flat": 0, "cn_down_us_up": 2, "cn_down_us_down": 1,
                "cn_up_us_down": -1, "cn_up_us_up": -2}},
            "thresholds": {"unused": [[-99, 99, 0]]},
            "verdicts": [{"min_total": -99, "verdict": "x", "action": "y"}],
        }
        return dual, bond

    def test_cn_flat_us_up_differs_between_the_two(self):
        """中债不动 + 美债上行：债基判 0（冲击有限），恒科判 -1（分母端占主导）。

        这正是「不能照抄判定表」的地方——债基定价锚在国内，恒科分母端在美元。
        """
        cn = _series([1.70] + [1.702] * 20)     # +0.2bp → flat
        us = _series([4.50] + [4.80] * 20)      # +30bp → up
        dual, bond = self._both_defs()

        r_dual = _run(StubEngine(cn10=cn, us10=us), dual)
        r_bond = _run(StubEngine(cn10=cn, us10=us), bond)

        assert _only(r_dual)["value"] == -1, "恒科：美债收紧应占主导"
        assert _only(r_bond)["value"] == 0, "债基：中债不动=冲击有限"

    def test_cn_up_us_up_differs_between_the_two(self):
        """中债上行 + 美债上行：债基判 -2（内外双紧），恒科判 0（拉锯）。

        恒科的中国利率上行是**分子端利好**（复苏），与债基完全相反。
        """
        cn = _series([1.70] + [1.80] * 20)      # +10bp → up
        us = _series([4.50] + [4.80] * 20)      # +30bp → up
        dual, bond = self._both_defs()

        r_dual = _run(StubEngine(cn10=cn, us10=us), dual)
        r_bond = _run(StubEngine(cn10=cn, us10=us), bond)

        assert _only(r_dual)["value"] == 0, "恒科：中国复苏 vs 美债收紧 = 拉锯"
        assert _only(r_bond)["value"] == -2, "债基：内外双紧"


class TestShippedHangSengDefinition:
    """真实定义文件本身：结构、校验、以及「不照抄别家」的守卫。"""

    @pytest.fixture(scope="class")
    def defn(self):
        return json.loads(SENG_DEF.read_text(encoding="utf-8"))

    def test_file_exists_and_validates(self, defn):
        assert not defs.validate_def(defn), defs.validate_def(defn)

    def test_registered_in_list_defs(self):
        assert "hang_seng_tech" in defs.list_defs()

    def test_subject_uses_hstech_with_hsi_benchmark(self, defn):
        s = defn["subject"]
        assert s["index_code"] == "HSTECH"
        assert s["benchmark_code"] == "HSI"

    def test_only_registered_calculators_are_used(self, defn):
        for f in defn["factors"]:
            assert f["compute"] in ThesisEngine.FACTOR_COMPUTERS, f["compute"]

    def test_every_threshold_key_used_by_a_factor_exists(self, defn):
        tables = defn.get("thresholds") or {}
        for f in defn["factors"]:
            if f.get("thresholds_key"):
                assert f["thresholds_key"] in tables, f["key"]

    def test_cn_axis_is_higher_is_better_only_where_intended(self, defn):
        """恒科台账里**没有**把中国利率写成 higher_is_better=false——
        一旦有人照抄红利/债基的符号，这里会先失败。"""
        for f in defn["factors"]:
            if f.get("series") == "cn10_y":
                assert f.get("higher_is_better") is not False, \
                    "中国利率对恒科是正相关（分子端），不该标成「越低越好」"

    def test_us_axis_is_lower_is_better(self, defn):
        f = [x for x in defn["factors"] if x["key"] == "us10_y"][0]
        assert f["higher_is_better"] is False

    def test_matrix_covers_all_nine_directions(self, defn):
        m = defn["matrices"]["dual_axis"]
        need = {"num_%s_denom_%s" % (a, b)
                for a in ("up", "down", "flat") for b in ("up", "down", "flat")}
        missing = need - set(m)
        assert not missing, f"判定矩阵缺方向组合：{sorted(missing)}"

    def test_nasdaq_and_southbound_are_deliberately_absent(self, defn):
        """研究报告证伪了两个主流叙事，不应被当作因子加回来。"""
        blob = json.dumps(defn, ensure_ascii=False)
        keys = {f["key"] for f in defn["factors"]}
        assert "nasdaq" not in keys and "southbound" not in keys
        assert "纳斯达克" in blob and "南向资金" in blob, "应在说明里写清为何排除"

    def test_verdict_scale_matches_factor_count(self, defn):
        """verdicts 量纲必须跟着因子数走：5 因子时满分 ±10（档位 5/2/−1/−4），
        加到 7 因子（±14）后应等比放大到 7/3/−1/−6。

        因子数变了而 verdicts 不变，会让「偏有利」的门槛相对变松——判定整体失真。
        """
        assert defn["version"] == "1.2"
        mins = [v["min_total"] for v in defn["verdicts"]]
        assert mins[0] == 7 and 3 in mins and -6 in mins, mins

    def test_threshold_evolution_is_documented(self, defn):
        """三处阈值与原设计（独立仪表盘的 config.json）不一致，必须在定义里写明沿革——
        否则下一个人会当成抄录笔误去「修正」，而它其实是有意的重新校准。"""
        blob = defn.get("_说明_阈值沿革", "")
        for token in ("3.8→3.0", "5pp→15pp", "4 档改 5 档"):
            assert token in blob, "阈值沿革缺少关键差异项: %s" % token

    def test_manual_block_still_declares_staleness_fields(self, defn):
        """凡保留 manual 块的台账，每项都必须带 updated_at + stale_days，
        否则「手工数据永久有效」这个隐患会静默存在。

        v1.1 起本台账已无手工项，故改为「有则必检」——
        直接断 defn["manual"] 会在去手工化之后误报，那是在编码旧结构而非保护语义。
        """
        for k, v in (defn.get("manual") or {}).items():
            if k.startswith("_"):
                continue
            assert v.get("updated_at") and v.get("stale_days"), k

    def test_dollar_liquidity_no_longer_duplicates_us10_y(self, defn):
        """⑤ 曾用手工项重复表达「美债趋势性站稳 5% 以上」，与 ① 的美债水平同源
        （② 又取美债变动），同一变量被计三遍，把 −1 级信号放大成「不利」。

        v1.1 改用港元 HIBOR 后，⑤ 只承担「联系汇率下的港元流动性」这一独立维度。
        本用例守住两件事：⑤ 不再是手工项，且不引用任何美债字段。
        """
        assert "dollar_liq" not in (defn.get("manual") or {}), "⑤ 不应再是手工项"
        hit = [f for f in defn["factors"] if f["key"] == "hk_liq"]
        assert hit, "⑤ 应为 hk_liq 自动因子"
        assert hit[0]["series"] == "hk_hibor_1w"
        assert "us10" not in json.dumps(hit[0], ensure_ascii=False), \
            "⑤ 不得引用美债（与 ① 重复计分）"


class TestShippedDefinitionComputes:
    """真实定义 + 桩数据端到端跑通（离线）。"""

    class _FullStub(ThesisEngine):
        def __init__(self, cn10, us10, hstech, hsi, hk_hibor=None, dxy=None):
            super().__init__()
            self._cn10, self._us10 = cn10, us10
            self._hstech, self._hsi = hstech, hsi
            # ⑤ 港元 HIBOR 与 ⑥ 合成美元指数都必须给独立桩：否则会落进 macro_series 的
            # cn10 兜底分支，出现「因子算出来了、其实拿中国国债序列在算」的假通过。
            self._hk_hibor = hk_hibor if hk_hibor is not None else _series([1.39] * 21)
            # 近 20 日 +98bp（96.60 → 97.58）→ 美元走强 → ⑥ 应为 −1
            self._dxy = dxy if dxy is not None else _series([96.60] + [97.58] * 20)

        def today(self):
            return TODAY

        def macro_stale_threshold(self, code):
            return 10

        def macro_series(self, code, periods=400):
            key = str(code)
            if key == "us10_y":
                return self._us10[-periods:]
            if key == "hk_hibor_1w":
                return self._hk_hibor[-periods:]
            if key == "dxy":
                return self._dxy[-periods:]
            return self._cn10[-periods:]

        def read_index_daily(self, code, days):
            return self._hstech if str(code).upper().startswith("HSTECH") else self._hsi

        def collect_index_daily(self, code, days, name=""):
            return 0

    def _engine(self):
        # 恒科 250 日在均线下 6% 附近；恒指更强 → 恒科相对偏弱
        hstech = _index_df([100.0] * 940 + [93.0] * 60)
        hsi = _index_df([100.0] * 940 + [101.5] * 60)
        return self._FullStub(
            cn10=_series([1.69] + [1.695] * 20),
            us10=_series([4.70] + [5.00] * 20),
            hstech=hstech, hsi=hsi)

    def test_computes_all_factors_without_warnings(self):
        """v1.2 起 7 因子（v1.1 的 5 个 + 补回的美元指数趋势、港美利差）。"""
        res = _run(self._engine(), json.loads(SENG_DEF.read_text(encoding="utf-8")))
        assert res["factor_count"] == 7
        assert res["valid_count"] == 7, [(f["key"], f["detail"]) for f in res["factors"]]
        assert not res["warnings"], res["warnings"]

    def test_high_us_yield_and_rising_drags_total_down(self):
        res = _run(self._engine(), json.loads(SENG_DEF.read_text(encoding="utf-8")))
        by = {f["key"]: f for f in res["factors"]}
        assert by["us10_y"]["score"] == -2       # 5.00% 落在 [4.8,5.2) 且趋势上行 → 再降一档
        assert by["dual_axis"]["score"] == -1    # 中国 flat + 美债 up
        assert res["total"] < 0

    def test_crowding_notes_absent_dividend_source(self):
        res = _run(self._engine(), json.loads(SENG_DEF.read_text(encoding="utf-8")))
        c = [f for f in res["factors"] if f["key"] == "crowding"][0]
        assert "无股息率数据源" in c["detail"]
        assert "股息率分位样本" not in c["detail"]

    def test_usd_trend_direction(self):
        """⑥ 美元趋势：近 20 日走强 → −1（压制港股）；走弱 → +1。

        美元指数只有**变动幅度**可比（合成口径的绝对水平含中间价政策因子偏差），
        所以阈值作用在 bp 上，本用例守住方向。
        """
        res = _run(self._engine(), json.loads(SENG_DEF.read_text(encoding="utf-8")))
        f = {x["key"]: x for x in res["factors"]}
        assert f["usd_trend"]["score"] == -1, f["usd_trend"]["detail"]
        assert "+98" in f["usd_trend"]["detail"]

        eng = self._engine()
        eng._dxy = _series([97.58] + [96.60] * 20)     # 反向：近 20 日走弱
        res2 = _run(eng, json.loads(SENG_DEF.read_text(encoding="utf-8")))
        f2 = {x["key"]: x for x in res2["factors"]}
        assert f2["usd_trend"]["score"] == 1, f2["usd_trend"]["detail"]

    def test_hk_us_spread_threshold_is_not_monotonic(self):
        """⑦ 港美利差阈值**非单调**：深度倒挂（≤−250bp）判 0 而非 +1。

        港元利率被压得异常低是套息外流压力的表征，不是「宽松红利」——这是原设计的
        刻意选择（config.json 的「港美利差_bp」本就是四段非单调），照抄「越低越好」
        的单调写法会把它抄错，故用两个档位钉住。
        """
        eng = self._engine()
        eng._hk_hibor = _series([1.39] * 21)
        eng._us10 = _series([5.00] * 21)               # 利差 = 139 − 500 = −361bp
        res = _run(eng, json.loads(SENG_DEF.read_text(encoding="utf-8")))
        sp = {x["key"]: x for x in res["factors"]}["hk_us_spread"]
        assert abs(float(sp["value"]) + 361.0) < 1.0, sp["value"]
        assert sp["score"] == 0, sp["detail"]          # 深度倒挂 → 0，不是 +1

        eng2 = self._engine()
        eng2._hk_hibor = _series([4.00] * 21)
        eng2._us10 = _series([5.00] * 21)              # 温和倒挂 −100bp → +1
        res2 = _run(eng2, json.loads(SENG_DEF.read_text(encoding="utf-8")))
        sp2 = {x["key"]: x for x in res2["factors"]}["hk_us_spread"]
        assert sp2["score"] == 1, sp2["detail"]

    def test_hk_liquidity_reads_hibor_series_not_the_cn10_fallback(self):
        """⑤ 必须真的吃港元 HIBOR 桩数据（1.39），不能落进 macro_series 的 cn10 兜底。

        1.39 与兜底的 1.69 虽然都落在「宽松」档，但读数不同 → 用 value 断言即可
        把「喂错序列」这类假通过钉死。
        """
        res = _run(self._engine(), json.loads(SENG_DEF.read_text(encoding="utf-8")))
        hk = [f for f in res["factors"] if f["key"] == "hk_liq"][0]
        assert abs(float(hk["value"]) - 1.39) < 1e-9, hk["value"]
        assert hk["score"] == 2, hk["detail"]
        assert hk["manual"] is False

    def test_tight_hk_hibor_drags_score_down(self):
        """HIBOR 升破 p90（3.80）→ 港元流动性收紧 → −2。
        守住阈值方向：HIBOR 越低越有利（higher_is_better=False）。"""
        eng = self._engine()
        eng._hk_hibor = _series([4.20] * 21)
        res = _run(eng, json.loads(SENG_DEF.read_text(encoding="utf-8")))
        hk = [f for f in res["factors"] if f["key"] == "hk_liq"][0]
        assert hk["score"] == -2, hk["detail"]

"""台账因子计算引擎测试 —— 取数层整体替换，完全离线。

覆盖：
- 区间打分与趋势修正（国债因子会因趋势上下调一档）
- 「数据缺失 ≠ 中性」契约：取不到数必须 score=None，不能给 0 分
- 手工项陈旧判定与 warnings 点名
- 相对收益 / 均线偏离度计算
- optional 信号（股息率分位）样本不足时不展示
- 结论失效条件命中
"""
from datetime import date, timedelta

import pandas as pd
import pytest

from thesis.engine import ThesisEngine

TODAY = date(2026, 9, 17)
END = date(2026, 9, 16)


def _series(values, end=END):
    """构造升序 [{report_date, value}]，末期为 end。"""
    n = len(values)
    return [{"report_date": (end - timedelta(days=n - 1 - i)).isoformat(), "value": v}
            for i, v in enumerate(values)]


def _index_df(closes, end=END, amounts=None):
    """指数日线桩。`amounts` 非空时补出成交额列（资金活跃度因子需要）。"""
    n = len(closes)
    data = {
        "code": ["X"] * n,
        "trade_date": [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)],
        "close": [float(c) for c in closes],
    }
    if amounts is not None:
        data["amount"] = [float(a) for a in amounts]
    return pd.DataFrame(data)


def _test_defn() -> dict:
    """测试自有定义：阈值简单直观，避免与真实阈值调参耦合。

    采用与生产一致的**声明式**形态（data + factors），因子计算器从注册表分发。
    """
    return {
        "id": "t_env",
        "name": "测试因子包",
        "version": "1.0",
        "asset_label": "测试指数",
        "subject": {"index_code": "H30269", "index_name": "红利低波",
                    "benchmark_code": "000300", "benchmark_name": "沪深300"},
        "data": {
            "dy":    {"kind": "index_valuation", "index_code": "H30269", "field": "dy"},
            "cn10_y": {"kind": "macro", "code": "cn10_y"},
            "red":   {"kind": "index_daily", "code": "H30269", "label": "红利低波指数"},
            "base":  {"kind": "index_daily", "code": "000300", "label": "基准指数"},
        },
        "factors": [
            {"key": "premium", "compute": "rate_premium", "name": "① 股息率溢价",
             "dy": "dy", "rate": "cn10_y", "metric": "premium", "unit": "pp",
             "thresholds_key": "premium_pp", "source": "测试来源"},
            {"key": "cn10_y", "compute": "rate_level_trend", "name": "② 10Y 国债",
             "series": "cn10_y", "code": "cn10_y", "metric": "cn10_y", "unit": "%",
             "thresholds_key": "cn10_y_pct", "higher_is_better": False, "source": "测试来源"},
            {"key": "insurance_gap", "compute": "manual", "name": "③ 险资 / OCI 流入",
             "manual": "insurance_oci", "value_field": "gap_yi", "metric": "insurance_gap",
             "stale_metric": "insurance_stale", "unit": "亿元",
             "thresholds_key": "insurance_gap_yi",
             "detail_template": "欠配 {gap_yi} 亿元；最近一季增配 {quarter_added_yi} 亿元；FVOCI 股票占比 {fvoci_stock_pct}%",
             "source": "手动配置"},
            {"key": "style_rel", "compute": "style_relative", "name": "④ 成长风格相对强弱",
             "red": "red", "base": "base", "metric": "style_rel", "unit": "pp",
             "thresholds_key": "style_rel_pp", "source": "测试来源"},
            {"key": "crowding", "compute": "crowding", "name": "⑤ 拥挤度",
             "red": "red", "dy": "dy", "metric": "crowding", "rank_metric": "dy_rank",
             "unit": "%偏离", "thresholds_key": "crowding_ext_pct", "source": "测试来源"},
        ],
        "params": {"trend_lookback_days": 20, "trend_move_bp": 10,
                   "style_lookback_days": 60, "style_lookback_short_days": 20,
                   "ma_days": 750, "min_samples_for_dy_rank": 120,
                   "index_hist_days": 900, "index_stale_days": 5},
        "thresholds": {
            "premium_pp": [[3.0, 99, 2], [2.0, 3.0, 1], [1.0, 2.0, 0], [-99, 1.0, -2]],
            "cn10_y_pct": [[-99, 1.70, 1], [1.70, 2.00, 0], [2.00, 99, -2]],
            "insurance_gap_yi": [[10000, 1e9, 1], [-99, 10000, -1]],
            "style_rel_pp": [[5, 999, 2], [-5, 5, 0], [-999, -5, -2]],
            "crowding_ext_pct": [[-999, -10, 1], [-10, 10, 0], [10, 999, -2]],
        },
        "manual": {"insurance_oci": {
            "label": "③ 险资 / OCI 流入", "updated_at": "2026-09-17",
            "gap_yi": 15000, "quarter_added_yi": 2885, "fvoci_stock_pct": 5.4,
            "stale_days": 120, "source_note": "测试来源"}},
        "verdicts": [{"min_total": 5, "verdict": "偏有利", "action": "分批买入"},
                     {"min_total": 2, "verdict": "中性偏有利", "action": "可持有"},
                     {"min_total": -1, "verdict": "中性", "action": "观望"},
                     {"min_total": -99, "verdict": "不利", "action": "不建议买入"}],
        "signals": {
            "buy": [{"metric": "premium", "op": ">", "value": 2.0, "label": "溢价>2"}],
            "warn": [{"metric": "cn10_y", "op": ">", "value": 2.0, "label": "国债>2"}],
        },
        "invalidation": {"rules": [
            {"metric": "cn10_y", "op": ">", "value": 2.10, "text": "国债升破 2.1%"}]},
    }


def _defn_with_optional_dy_rank() -> dict:
    d = _test_defn()
    d["signals"]["buy"].append(
        {"metric": "dy_rank", "op": ">", "value": 70, "optional": True, "label": "股息率分位>70"})
    d["signals"]["warn"].append(
        {"metric": "dy_rank", "op": "<", "value": 30, "optional": True, "label": "股息率分位<30"})
    return d


class StubEngine(ThesisEngine):
    """把取数层换成内存数据。"""

    def __init__(self, *, dy=None, cn10=None, red=None, base=None,
                 today_=TODAY, index_ok=True):
        super().__init__()
        self._dy = dy or []
        self._cn10 = cn10 or []
        self._red = red if red is not None else pd.DataFrame()
        self._base = base if base is not None else pd.DataFrame()
        self._today = today_
        self._index_ok = index_ok
        self.collected: list[str] = []

    def today(self):
        return self._today

    def macro_stale_threshold(self, code):
        return 10

    def macro_series(self, code, periods=60):
        src = self._dy if str(code).startswith("idx_") else self._cn10
        return src[-periods:]

    def sync_index_valuation(self, index_code):
        return {"dy_code": f"idx_{index_code.lower()}_dy",
                "pe_code": f"idx_{index_code.lower()}_pe",
                "dy_added": 0, "ok": self._index_ok}

    def read_index_daily(self, code, days):
        return self._red if str(code).startswith("H") else self._base

    def collect_index_daily(self, code, days, name=""):
        self.collected.append(code)
        return 0


def _by_key(res: dict) -> dict:
    return {f["key"]: f for f in res["factors"]}


def _flat(n, v):
    return [v] * n


class TestFactorScores:
    def test_readings_and_total(self):
        eng = StubEngine(dy=_series(_flat(20, 4.30)), cn10=_series(_flat(60, 1.6862)),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        res = eng.compute("t_env", _test_defn())
        f = _by_key(res)

        assert f["premium"]["value"] == pytest.approx(2.6138)
        assert f["premium"]["score"] == 1          # [2.0, 3.0) → 1
        assert f["cn10_y"]["score"] == 1           # <1.70 → 1，趋势平稳不加分
        assert f["insurance_gap"]["score"] == 1    # 15000 ≥ 10000
        assert f["style_rel"]["score"] == 0        # 相对收益 0
        assert f["crowding"]["score"] == 0         # 延伸度 0

        assert res["total"] == 3
        assert res["valid_count"] == 5
        assert res["verdict"] == "中性偏有利"
        assert res["action"] == "可持有"

    def test_missing_data_scores_none_not_zero(self):
        """核心契约：取不到数据必须 score=None；给 0 分会被误读成「中性」。"""
        res = StubEngine().compute("t_env", _test_defn())
        f = _by_key(res)

        for key in ("premium", "cn10_y", "style_rel", "crowding"):
            assert f[key]["score"] is None, key
            assert f[key]["status"] == "数据缺失", key
        assert f["insurance_gap"]["score"] == 1     # 手工项仍有值
        assert res["valid_count"] == 1
        assert res["total"] == 1
        assert res["verdict"] == "中性"             # 不是「数据不足」（有 1 个有效因子）
        assert res["warnings"]

    def test_all_missing_verdict_is_insufficient(self):
        d = _test_defn()
        d["manual"]["insurance_oci"]["gap_yi"] = None
        res = StubEngine().compute("t_env", d)
        assert res["valid_count"] == 0
        assert res["verdict"] == "数据不足"
        assert res["action"] == "无法判断"

    def test_index_valuation_sync_failure_adds_warning(self):
        res = StubEngine(index_ok=False).compute("t_env", _test_defn())
        assert any("指数估值同步失败" in w for w in res["warnings"])


class TestBondTrendCorrection:
    # 序列 40 项起点值 + 20 项现值：series[-21] 恰好落在第 40 项（= 21 个交易日前）
    def test_downtrend_upgrades_one_notch(self):
        series = _series(_flat(40, 1.80) + _flat(20, 1.6862))
        eng = StubEngine(cn10=series, red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        f = _by_key(eng.compute("t_env", _test_defn()))
        assert f["cn10_y"]["score"] == 2
        assert "下行" in f["cn10_y"]["detail"]

    def test_uptrend_downgrades_one_notch(self):
        series = _series(_flat(40, 1.60) + _flat(20, 1.75))
        eng = StubEngine(cn10=series, red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        f = _by_key(eng.compute("t_env", _test_defn()))
        assert f["cn10_y"]["score"] == -1
        assert "上行" in f["cn10_y"]["detail"]

    def test_small_move_stays_flat(self):
        series = _series(_flat(40, 1.69) + _flat(20, 1.6862))
        eng = StubEngine(cn10=series, red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        f = _by_key(eng.compute("t_env", _test_defn()))
        assert f["cn10_y"]["score"] == 1
        assert "平稳" in f["cn10_y"]["detail"]

    def test_trend_needs_enough_history(self):
        """序列不足 trend_lookback+1 时不做趋势修正，也不报错。"""
        eng = StubEngine(cn10=_series(_flat(10, 1.6862)),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        f = _by_key(eng.compute("t_env", _test_defn()))
        assert f["cn10_y"]["score"] == 1
        assert "下行" not in f["cn10_y"]["detail"]


class TestManualInsurance:
    def test_stale_manual_item_flagged_and_named(self):
        d = _test_defn()
        d["manual"]["insurance_oci"]["updated_at"] = "2025-01-01"
        res = StubEngine().compute("t_env", d)
        f = _by_key(res)
        assert f["insurance_gap"]["stale"] is True
        assert res["metric_values"]["insurance_stale"] == 1
        assert any("数据更新于" in w and "险资" in w for w in res["warnings"])

    def test_fresh_manual_item_not_stale(self):
        res = StubEngine().compute("t_env", _test_defn())
        assert _by_key(res)["insurance_gap"]["stale"] is False
        assert res["metric_values"]["insurance_stale"] == 0

    def test_source_note_surfaced(self):
        res = StubEngine().compute("t_env", _test_defn())
        assert _by_key(res)["insurance_gap"]["note"] == "测试来源"

    def test_missing_gap_still_reports_staleness(self):
        d = _test_defn()
        d["manual"]["insurance_oci"]["gap_yi"] = None
        d["manual"]["insurance_oci"]["updated_at"] = "2025-01-01"
        res = StubEngine().compute("t_env", d)
        assert _by_key(res)["insurance_gap"]["score"] is None
        assert res["metric_values"]["insurance_stale"] == 1


class TestStyleAndCrowding:
    def test_relative_excess_return(self):
        # 红利低波日涨 0.1%，基准走平 → 近 60 日相对 +6.18pp
        red = _index_df([1000.0 * (1.001 ** i) for i in range(200)])
        base = _index_df(_flat(200, 1000.0))
        res = StubEngine(red=red, base=base).compute("t_env", _test_defn())
        f = _by_key(res)
        assert f["style_rel"]["value"] == pytest.approx(6.17, abs=0.05)
        assert f["style_rel"]["score"] == 2

    def test_crowding_ma_deviation(self):
        # 199 项 100 + 末项 150 → 均线 100.25，延伸度 = 150/100.25 - 1 = +49.63%
        closes = _flat(199, 100.0) + [150.0]
        res = StubEngine(red=_index_df(closes),
                         base=_index_df(_flat(200, 100.0))).compute("t_env", _test_defn())
        f = _by_key(res)
        assert f["crowding"]["value"] == pytest.approx(49.63, abs=0.05)
        assert f["crowding"]["score"] == -2
        assert "均线" in f["crowding"]["detail"]

    def test_crowding_needs_min_rows(self):
        res = StubEngine(red=_index_df(_flat(50, 100.0)),
                         base=_index_df(_flat(50, 100.0))).compute("t_env", _test_defn())
        f = _by_key(res)
        assert f["crowding"]["score"] is None
        assert any("历史不足" in w for w in res["warnings"])

    def test_rows_enough_but_stale_triggers_collect_and_warning(self):
        """库里 200 行但停在两周前 → 必须补数并告警。

        只查行数会让「成长风格相对强弱」拿过期数据算出一个看起来很正常的分数，
        对跟踪台账是硬伤（曾真实发生：基准只到 2026-09-10 而当日是 09-17）。
        """
        stale_end = date(2026, 9, 1)
        eng = StubEngine(red=_index_df(_flat(200, 1000.0), end=stale_end),
                         base=_index_df(_flat(200, 1000.0), end=stale_end))
        res = eng.compute("t_env", _test_defn())

        assert eng.collected == ["H30269", "000300"]   # 两个指数都被判定需补数
        assert any("未更新" in w for w in res["warnings"])

    def test_fresh_index_data_does_not_trigger_collect(self):
        """数据已到「最近应有交易日」→ 不该采集。

        注意夹具日期：TODAY 是 2026-09-17（周四），所以「新鲜」= 日线到 09-17。
        用默认的 END（09-16）会被判为落后一天而触发采集——那不是 bug，
        见下面 test_index_one_day_behind_triggers_collect 的说明。
        """
        fresh_end = TODAY          # 2026-09-17，周四，最近应有交易日
        eng = StubEngine(red=_index_df(_flat(200, 1000.0), end=fresh_end),
                         base=_index_df(_flat(200, 1000.0), end=fresh_end))
        eng.compute("t_env", _test_defn())
        assert eng.collected == []

    def test_index_one_day_behind_triggers_collect(self):
        """只落后一天也要补数（2026-09-18 真实事故的边界）。

        旧实现拿 `index_stale_days`（默认 5 天）当补数开关，于是落后 1~5 天都不刷新，
        页面点刷新永远看到同一批数据、趋势图只有一个点。补数判定改用
        「落后于最近应有交易日」后，这里必须触发采集。
        """
        eng = StubEngine(red=_index_df(_flat(200, 1000.0), end=END),      # 09-16
                         base=_index_df(_flat(200, 1000.0), end=END))
        eng.compute("t_env", _test_defn())
        assert eng.collected == ["H30269", "000300"]

    def test_style_needs_common_dates(self):
        """两个指数没有足够重叠交易日 → 相对收益给不出，但不能崩。"""
        res = StubEngine(red=_index_df(_flat(200, 100.0)),
                         base=_index_df(_flat(30, 100.0))).compute("t_env", _test_defn())
        assert _by_key(res)["style_rel"]["score"] is None


class TestSignals:
    def test_signal_hit_and_miss(self):
        eng = StubEngine(dy=_series(_flat(20, 4.30)), cn10=_series(_flat(60, 1.6862)),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        sig = eng.compute("t_env", _test_defn())["signals"]
        assert sig["buy"][0]["label"] == "溢价>2"
        assert sig["buy"][0]["hit"] is True          # 2.61 > 2.0
        assert sig["buy"][0]["current"] == "+2.61pp"
        assert sig["warn"][0]["hit"] is False        # 1.6862 不 > 2.0

    def test_optional_signal_skipped_without_samples(self):
        """股息率分位样本不足时应整条不展示，而不是永远显示「未命中」。"""
        eng = StubEngine(dy=_series(_flat(20, 4.30)),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        sig = eng.compute("t_env", _defn_with_optional_dy_rank())["signals"]
        assert len(sig["buy"]) == 1
        assert all("分位" not in r["label"] for r in sig["buy"] + sig["warn"])

    def test_optional_signal_included_with_samples(self):
        eng = StubEngine(dy=_series([3.0 + i * 0.01 for i in range(150)]),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        res = eng.compute("t_env", _defn_with_optional_dy_rank())
        assert res["metric_values"]["dy_rank"] == 100.0
        buy = [r for r in res["signals"]["buy"] if "分位" in r["label"]]
        assert buy and buy[0]["hit"] is True

    def test_dy_rank_none_below_threshold(self):
        eng = StubEngine(dy=_series([3.0 + i * 0.01 for i in range(119)]))
        res = eng.compute("t_env", _defn_with_optional_dy_rank())
        assert res["metric_values"]["dy_rank"] is None


class TestInvalidation:
    def test_invalidation_hit(self):
        eng = StubEngine(cn10=_series(_flat(60, 2.20)),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        inv = eng.compute("t_env", _test_defn())["invalidation"]
        assert len(inv) == 1
        assert inv[0]["hit"] is True
        assert inv[0]["current"] == "2.2000%"

    def test_invalidation_not_hit(self):
        eng = StubEngine(cn10=_series(_flat(60, 1.6862)),
                         red=_index_df(_flat(200, 1000.0)),
                         base=_index_df(_flat(200, 1000.0)))
        inv = eng.compute("t_env", _test_defn())["invalidation"]
        assert inv[0]["hit"] is False

    def test_invalidation_missing_metric_not_hit(self):
        res = StubEngine().compute("t_env", _test_defn())
        assert res["invalidation"][0]["hit"] is False
        assert res["invalidation"][0]["current"] == "—"


class TestVerdictBuckets:
    @pytest.mark.parametrize("total,expected", [
        (6, "偏有利"), (5, "偏有利"), (4, "中性偏有利"), (2, "中性偏有利"),
        (1, "中性"), (0, "中性"), (-1, "中性"), (-2, "不利"), (-10, "不利"),
    ])
    def test_bucket(self, total, expected):
        verdicts = _test_defn()["verdicts"]
        verdict, _ = ThesisEngine._verdict(total, 4, verdicts)
        assert verdict == expected

    def test_no_valid_factors(self):
        verdict, action = ThesisEngine._verdict(0, 0, _test_defn()["verdicts"])
        assert verdict == "数据不足"
        assert action == "无法判断"


class TestDefinitionProblemsSurface:
    def test_broken_definition_problems_become_warnings(self, monkeypatch):
        """阈值没兜住会被误当成「数据缺失」——必须把定义问题点出来，否则排查方向全错。"""
        from thesis import defs as thesis_defs
        monkeypatch.setattr(thesis_defs, "load_def_checked", lambda tid: (
            _test_defn(), ["[t_env] 阈值 premium_pp 未覆盖低端（最小下限 1.0 > -99）"]))
        eng = StubEngine()
        res = eng.compute("t_env")
        assert any("未覆盖低端" in w for w in res["warnings"])

    def test_missing_definition_raises(self, monkeypatch):
        from thesis import defs as thesis_defs
        monkeypatch.setattr(thesis_defs, "load_def_checked", lambda tid: (None, ["不存在"]))
        with pytest.raises(ValueError):
            StubEngine().compute("nope")


class TestRealDefinitionOffline:
    def test_real_dividend_env_wires_up(self):
        """用真实 data/thesis/dividend_env.json 跑一遍（取数层仍为内存数据）。

        v1.2 起 7 因子（+ ⑥ M1−M2 剪刀差、+ ⑦ 资金活跃度）。
        """
        n = 200
        eng = StubEngine(dy=_series(_flat(20, 4.27)), cn10=_series(_flat(60, 1.6862)),
                         red=_index_df(_flat(n, 1000.0), amounts=range(1, n + 1)),
                         base=_index_df(_flat(n, 1000.0)))
        # m1/m2 必须给独立桩：否则会落进 cn10 兜底 → 剪刀差恒等于 0（假通过）
        _m1, _m2 = _series(_flat(60, 4.10)), _series(_flat(60, 7.50))

        def _ms(code, periods=400):
            k = str(code)
            if k == "m1_yoy":
                return _m1[-periods:]
            if k == "m2_yoy":
                return _m2[-periods:]
            return eng._cn10[-periods:]

        eng.macro_series = _ms
        res = eng.compute("dividend_env")
        f = _by_key(res)
        assert res["thesis_id"] == "dividend_env"
        assert res["factor_count"] == 7
        assert res["valid_count"] == 7, [(x["key"], x["detail"]) for x in res["factors"]]
        # 新因子的读数来自各自的数据源，不是兜底序列
        assert f["m1m2_gap"]["value"] == pytest.approx(-3.4)
        assert f["fund_flow"]["value"] is not None      # 成交额递增 → 分位应为高分
        # 真实定义本身必须是干净的（validate_def 无问题）
        assert not any("未覆盖" in w for w in res["warnings"])
        assert not any("未知指标" in w for w in res["warnings"])
        assert not any("未注册" in w for w in res["warnings"])
        # 信号与失效条件都被求值过
        assert res["signals"]["buy"] and res["signals"]["warn"]
        assert res["invalidation"]


# ---------------------------------------------------------------------------
# 债券基金台账（第二个台账，验证引擎的定义驱动可扩展性）
# ---------------------------------------------------------------------------

def _bond_defn() -> dict:
    return {
        "id": "t_bond", "name": "测试债基因子包", "version": "1.0",
        "asset_label": "测试债基",
        "subject": {"index_code": "H11001", "index_name": "中证全债"},
        "data": {
            "cn10_y": {"kind": "macro", "code": "cn10_y"},
            "us10_y": {"kind": "macro", "code": "us10_y"},
            "shibor_1w": {"kind": "macro", "code": "shibor_1w"},
        },
        "factors": [
            {"key": "coupon", "compute": "rate_level_trend", "name": "① 票息垫子",
             "series": "cn10_y", "code": "cn10_y", "metric": "cn10_y", "unit": "%",
             "thresholds_key": "cn10_y_bond_pct", "higher_is_better": False,
             "source": "测试"},
            {"key": "double_tighten", "compute": "double_tightening", "name": "② 内外双紧",
             "cn": "cn10_y", "us": "us10_y", "matrix": "double_tighten",
             "metric": "double_tighten", "cn_metric": "cn10_y", "us_metric": "us10_y",
             "cn_chg_metric": "cn10_chg_bp", "us_chg_metric": "us10_chg_bp",
             "lookback_days": 20, "move_bp": 10, "unit": "档", "source": "测试"},
            {"key": "funding", "compute": "rate_level_trend", "name": "③ 资金面",
             "series": "shibor_1w", "code": "shibor_1w", "metric": "shibor_1w",
             "unit": "%", "thresholds_key": "shibor_funding_pct",
             "higher_is_better": False, "trend_lookback_days": 10, "trend_move_bp": 5,
             "source": "测试"},
            {"key": "fx", "compute": "manual", "name": "④ 汇率约束",
             "manual": "fx", "value_field": "usdcny", "metric": "usdcny",
             "stale_metric": "fx_stale", "unit": "",
             "thresholds_key": "fx_usdcny", "detail_template": "USDCNY {usdcny}；{note}",
             "source": "手动"},
            {"key": "redemption", "compute": "manual", "name": "⑤ 赎回",
             "manual": "redemption", "value_field": "stress_level",
             "metric": "redemption_stress", "stale_metric": "redemption_stale",
             "unit": "级", "thresholds_key": "redemption_stress",
             "detail_template": "压力等级 {stress_level}", "source": "手动"},
            {"key": "gov_supply", "compute": "manual", "name": "⑥ 政府债供给",
             "manual": "gov_supply", "value_field": "net_supply_yi",
             "metric": "gov_supply_yi", "stale_metric": "gov_supply_stale",
             "unit": "亿元", "thresholds_key": "gov_supply_yi",
             "detail_template": "净融资 {net_supply_yi} 亿元", "source": "手动"},
        ],
        "params": {"trend_lookback_days": 20, "trend_move_bp": 10,
                   "funding_lookback_days": 10, "funding_move_bp": 5},
        "matrices": {"double_tighten": {"flat": 0, "cn_down_us_up": 2,
                                        "cn_down_us_down": 1,
                                        "cn_up_us_down": -1, "cn_up_us_up": -2}},
        "thresholds": {
            "cn10_y_bond_pct": [[-99, 1.65, 2], [1.65, 1.85, 1], [1.85, 2.00, 0],
                                [2.00, 2.30, -1], [2.30, 99, -2]],
            "shibor_funding_pct": [[-99, 1.60, 2], [1.60, 1.90, 1], [1.90, 2.30, 0],
                                   [2.30, 2.80, -1], [2.80, 99, -2]],
            "fx_usdcny": [[-99, 6.85, 2], [6.85, 7.15, 0], [7.15, 7.35, -1], [7.35, 99, -2]],
            "redemption_stress": [[-99, 0.5, 1], [0.5, 1.5, -1], [1.5, 99, -2]],
            "gov_supply_yi": [[-99, 8000, 1], [8000, 15000, 0], [15000, 1e9, -1]],
        },
        "manual": {
            "fx": {"label": "④ 汇率约束", "updated_at": "2026-09-17", "usdcny": 6.70,
                   "note": "偏升值一侧", "stale_days": 30, "source_note": "测试"},
            "redemption": {"label": "⑤ 赎回", "updated_at": "2026-09-17",
                           "stress_level": 0, "note": "无异常", "stale_days": 15,
                           "source_note": "测试"},
            "gov_supply": {"label": "⑥ 政府债供给", "updated_at": "2026-09-17",
                           "net_supply_yi": 17500, "wow_yi": 6400, "note": "供给放量",
                           "stale_days": 30, "source_note": "测试"},
        },
        "verdicts": [{"min_total": 6, "verdict": "对债基有利", "action": "可持有"},
                     {"min_total": 3, "verdict": "中性偏有利", "action": "可持有"},
                     {"min_total": -1, "verdict": "中性", "action": "观望"},
                     {"min_total": -99, "verdict": "不利", "action": "降敞口"}],
        "signals": {
            "buy": [{"metric": "double_tighten", "op": ">=", "value": 1,
                     "label": "国内独立宽松"}],
            "warn": [{"metric": "double_tighten", "op": "<=", "value": -1,
                      "label": "国内利率上行"}],
        },
        "invalidation": {"rules": [
            {"metric": "double_tighten", "op": "<=", "value": -2, "text": "内外双紧确立"},
            {"metric": "redemption_stress", "op": ">=", "value": 2, "text": "赎回负反馈"},
            {"metric": "fx_stale", "op": "is_true", "text": "汇率数据超期"},
        ]},
    }


def _bond_engine(cn10, us10, shibor, usdcny=None, bond_index=None, term_spread=None):
    """债基台账的桩引擎。

    `usdcny` / `bond_index` 必须显式分流：StubEngine 的兜底是「非 idx_ 一律当 cn10」、
    「H 开头一律当红利指数」，不接管就会出现**假通过**——
    汇率拿着 1.6862 当报价、赎回压力拿着 1000 点的平盘序列算债市冲击，
    两边都算得出分，测试全绿却什么也没验证。
    """
    eng = StubEngine(cn10=cn10, red=_index_df(_flat(200, 1000.0)),
                     base=_index_df(_flat(200, 1000.0)))
    fx = usdcny if usdcny is not None else _series(_flat(60, 6.75))
    bond = bond_index if bond_index is not None else _index_df(_flat(200, 1000.0))
    # 期限利差也要独立桩：漏了会落进 cn10 兜底（1.6862），被当成「曲线极陡」判 +2
    ts = term_spread if term_spread is not None else _series(_flat(60, 0.41))

    # StubEngine.macro_series 只分 idx_/其它两路，这里扩成五路
    def series(code, periods=60):
        key = str(code)
        if key.startswith("idx_"):
            return eng._dy[-periods:]
        if key == "us10_y":
            return us10[-periods:]
        if key == "shibor_1w":
            return shibor[-periods:]
        if key == "usdcny":
            return fx[-periods:]
        if key == "cn_term_spread":
            return ts[-periods:]
        return cn10[-periods:]

    eng.macro_series = series

    def read_index_daily(code, days):
        if str(code).upper() == "H11001":
            return bond
        return eng._red if str(code).startswith("H") else eng._base

    eng.read_index_daily = read_index_daily
    return eng


class TestDoubleTightening:
    """② 内外双紧：以中债为主轴的判定表。"""

    def test_flat_both(self):
        eng = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45)))
        f = _by_key(eng.compute("t_bond", _bond_defn()))["double_tighten"]
        assert f["score"] == 0
        assert "中债基本未动" in f["detail"]

    def test_cn_flat_us_up_is_neutral_not_double_tighten(self):
        """回归：中债没动 + 美债上行 ≠ 内外双紧。

        长周期复盘的核心结论就是「中国债市定价权在国内」——中债 +0.3bp 时
        美债即使上行 30bp 也只是情绪扰动，判成 -2 会把中性误报成不利
        （2026-09-17 真实数据就是这么误判的）。
        """
        eng = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(40, 4.90) + _flat(20, 5.01)),
                           _series(_flat(60, 1.45)))
        res = eng.compute("t_bond", _bond_defn())
        f = _by_key(res)["double_tighten"]
        assert f["score"] == 0
        assert "情绪扰动" in f["detail"]
        inv = [r for r in res["invalidation"] if "内外双紧" in r["text"]]
        assert inv and inv[0]["hit"] is False

    def test_cn_down_us_up_is_best(self):
        # 中债下行 15bp、美债上行 15bp → 外紧内松 → +2（利率单位是 %，15bp = 0.15）
        eng = _bond_engine(_series(_flat(40, 1.70) + _flat(20, 1.55)),
                           _series(_flat(40, 5.00) + _flat(20, 5.15)),
                           _series(_flat(60, 1.45)))
        f = _by_key(eng.compute("t_bond", _bond_defn()))["double_tighten"]
        assert f["score"] == 2
        assert "外紧内松" in f["detail"]

    def test_cn_down_us_down(self):
        eng = _bond_engine(_series(_flat(40, 1.70) + _flat(20, 1.55)),
                           _series(_flat(40, 5.02) + _flat(20, 4.87)),
                           _series(_flat(60, 1.45)))
        f = _by_key(eng.compute("t_bond", _bond_defn()))["double_tighten"]
        assert f["score"] == 1

    def test_cn_up_us_up_is_worst(self):
        # 内外双紧：中债上行 15bp + 美债上行 15bp → -2（历史债基亏钱组合）
        eng = _bond_engine(_series(_flat(40, 1.68) + _flat(20, 1.83)),
                           _series(_flat(40, 5.00) + _flat(20, 5.15)),
                           _series(_flat(60, 1.45)))
        res = eng.compute("t_bond", _bond_defn())
        f = _by_key(res)["double_tighten"]
        assert f["score"] == -2
        assert res["metric_values"]["double_tighten"] == -2
        inv = [r for r in res["invalidation"] if "内外双紧" in r["text"]]
        assert inv and inv[0]["hit"] is True

    def test_cn_up_us_flat(self):
        # 国内自身收紧（美债没动）→ -1
        eng = _bond_engine(_series(_flat(40, 1.68) + _flat(20, 1.83)),
                           _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45)))
        f = _by_key(eng.compute("t_bond", _bond_defn()))["double_tighten"]
        assert f["score"] == -1
        assert "国内自身收紧" in f["detail"]

    def test_insufficient_history_scores_none(self):
        eng = _bond_engine(_series(_flat(10, 1.69)), _series(_flat(10, 5.01)),
                           _series(_flat(10, 1.45)))
        res = eng.compute("t_bond", _bond_defn())
        f = _by_key(res)["double_tighten"]
        assert f["score"] is None
        assert res["metric_values"]["double_tighten"] is None


class TestBondOtherFactors:
    def test_coupon_and_funding_scores(self):
        # cn10_y 1.6862 → [1.65,1.85) → +1；shibor 1.45 → <1.60 → +2
        eng = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45)))
        res = eng.compute("t_bond", _bond_defn())
        f = _by_key(res)
        assert f["coupon"]["score"] == 1
        assert f["funding"]["score"] == 2
        assert f["funding"]["value"] == 1.45

    def test_funding_tightening_downgrades(self):
        # shibor 上行 8bp（>5bp 阈值）→ 资金面收敛 → 降一档；1.50 仍在 [1.60) 内 → 2-1=1
        # 注意 n=10：series[-11] 须落在前一段（50 个 1.42 + 10 个 1.50）
        eng = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(50, 1.42) + _flat(10, 1.50)))
        f = _by_key(eng.compute("t_bond", _bond_defn()))["funding"]
        assert f["score"] == 1
        assert "收敛/不利" in f["detail"]

    def test_manual_items_scored(self):
        eng = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45)))
        res = eng.compute("t_bond", _bond_defn())
        f = _by_key(res)
        assert f["fx"]["score"] == 2                # 6.70 < 6.85
        assert f["fx"]["stale"] is False
        assert f["redemption"]["score"] == 1        # 0 → 正常
        assert f["gov_supply"]["score"] == -1       # 17500 > 15000
        assert res["metric_values"]["usdcny"] == 6.70
        assert res["metric_values"]["gov_supply_stale"] == 0
        assert "17500" in f["gov_supply"]["detail"]

    def test_missing_manual_value_flags_stale(self):
        d = _bond_defn()
        d["manual"]["fx"]["usdcny"] = None
        res = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45))).compute("t_bond", d)
        f = _by_key(res)
        assert f["fx"]["score"] is None
        assert res["metric_values"]["fx_stale"] == 1
        assert any("汇率约束" in w for w in res["warnings"])

    def test_real_bond_def_wires_up(self):
        """真实 data/thesis/bond_fund.json 必须能离线跑通且定义干净。

        v1.2 起 7 因子（+ ⑦ 期限利差，作久期环境代理）。
        """
        eng = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.012)),
                           _series(_flat(60, 1.45)))
        res = eng.compute("bond_fund")
        assert res["thesis_id"] == "bond_fund"
        assert res["factor_count"] == 7
        assert not any("未注册" in w for w in res["warnings"])
        assert not any("未知指标" in w for w in res["warnings"])
        assert not any("未覆盖" in w for w in res["warnings"])
        assert res["valid_count"] == 7

        # v1.1：④⑤ 已接自动源，只有 ⑥ 是手工项。此处必须断言**读数来自对应序列**——
        # 只断 valid_count 的话，usdcny 拿到 cn10 的 1.6862、H11001 拿到红利指数的
        # 平盘序列也照样「6/6 有效」，等于什么都没验证。
        f = _by_key(res)
        assert f["fx"]["manual"] is False
        assert float(f["fx"]["value"]) == pytest.approx(6.75)
        assert f["redemption"]["manual"] is False
        assert "代理口径" in f["redemption"]["detail"]
        assert f["gov_supply"]["manual"] is True
        assert float(f["gov_supply"]["value"]) == pytest.approx(17500)
        # ⑦ 期限利差必须吃独立桩（0.41），不是 cn10 兜底的 1.6862
        assert float(f["term_spread"]["value"]) == pytest.approx(0.41)
        assert f["term_spread"]["score"] == 0

    def test_unregistered_compute_is_surfaced(self):
        d = _bond_defn()
        d["factors"][0]["compute"] = "no_such_computer"
        res = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45))).compute("t_bond", d)
        assert any("未注册" in w for w in res["warnings"])
        assert _by_key(res)["coupon"]["score"] is None


def _fx_defn() -> dict:
    """汇率类最小定义：阈值与生产一致，便于直读档位。"""
    return {
        "id": "t_fx", "name": "T", "version": "1",
        "subject": {"index_code": "X", "benchmark_code": "X"},
        "data": {"usdcny": {"kind": "macro", "code": "usdcny", "periods": 400}},
        "factors": [
            {"key": "fx", "compute": "level_trend_pct", "name": "汇率",
             "series": "usdcny", "metric": "usdcny", "unit": "CNY/USD",
             "thresholds_key": "fx", "higher_is_better": False,
             "trend_lookback_days": 20, "trend_move_pct": 0.5,
             "down_label": "人民币升值", "up_label": "人民币贬值"},
        ],
        "thresholds": {"fx": [[-99, 6.85, 2], [6.85, 7.15, 0],
                              [7.15, 7.35, -1], [7.35, 99, -2]]},
    }


def _shock_defn() -> dict:
    """债市冲击类最小定义（赎回压力代理）。"""
    return {
        "id": "t_shock", "name": "T", "version": "1",
        "subject": {"index_code": "H11001", "benchmark_code": "H11001"},
        "data": {"bond_index": {"kind": "index_daily", "code": "H11001", "days": 900}},
        "factors": [
            {"key": "redemption", "compute": "index_shock", "name": "赎回压力",
             "frame": "bond_index", "metric": "bond_shock_5d",
             "shock_lookback_days": 5, "unit": "%", "thresholds_key": "shock"},
        ],
        "params": {"shock_lookback_days": 5, "index_stale_days": 5},
        "thresholds": {"shock": [[-99, -0.50, -2], [-0.50, -0.20, -1], [-0.20, 99, 1]]},
    }


class TestLevelTrendPct:
    """④ 汇率：水平分 + **相对变动**趋势。

    与 `rate_level_trend` 的差别只在趋势口径（汇率不适用 bp 表述），
    因此这里要盯住「百分比阈值生效」而不是 bp。
    """

    def _eng(self, usdcny):
        return _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                            _series(_flat(60, 1.45)), usdcny=usdcny)

    def test_flat_quote_only_applies_level_score(self):
        f = _by_key(self._eng(_series(_flat(30, 6.75))).compute("t_fx", _fx_defn()))["fx"]
        assert f["score"] == 2                     # 6.75 < 6.85
        assert "平稳" in f["detail"]
        assert f["manual"] is False

    def test_depreciation_beyond_move_pct_drops_one_step(self):
        """20 日贬值 4.5%（≫0.5%）→ 水平 [6.85,7.15)=0，趋势降一档 = −1。"""
        f = _by_key(self._eng(_series([6.60] + [6.90] * 20))
                    .compute("t_fx", _fx_defn()))["fx"]
        assert f["score"] == -1
        assert "人民币贬值" in f["detail"]

    def test_appreciation_is_capped_at_two(self):
        """升值趋势在水平分已是 +2 时不得突破上限（SCORE_MAX=2）。"""
        f = _by_key(self._eng(_series([7.05] + [6.75] * 20))
                    .compute("t_fx", _fx_defn()))["fx"]
        assert f["score"] == 2
        assert "人民币升值" in f["detail"]

    def test_insufficient_history_stays_none(self):
        """历史不足一个趋势窗口时不猜趋势，但水平分照给。"""
        f = _by_key(self._eng(_series(_flat(10, 6.75))).compute("t_fx", _fx_defn()))["fx"]
        assert f["score"] == 2
        assert "近20日" not in f["detail"]

    def test_missing_series_is_none_not_zero(self):
        f = _by_key(self._eng(_series([])).compute("t_fx", _fx_defn()))["fx"]
        assert f["score"] is None
        assert f["status"] == "数据缺失"


class TestIndexShock:
    """⑤ 赎回压力代理：近 N 日涨跌幅 → 压力档。

    阈值锚自四次真实债市负反馈（2013-06 / 2016-12 / 2020-05 / 2022-11），
    常态 p50 仅 +0.11%，故 -0.20% / -0.50% 两档能把负反馈与日常波动分开。
    """

    def _eng(self, closes):
        return _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                            _series(_flat(60, 1.45)), bond_index=_index_df(closes))

    def test_calm_market_scores_positive(self):
        f = _by_key(self._eng(_flat(200, 1000.0))
                    .compute("t_shock", _shock_defn()))["redemption"]
        assert f["score"] == 1
        assert "代理口径" in f["detail"]
        assert f["manual"] is False

    def test_negative_feedback_scores_minus_two(self):
        """近 5 日 −0.6%：落在 2022-11 赎回潮那一档。"""
        f = _by_key(self._eng(_flat(200, 1000.0) + [994.0] * 5)
                    .compute("t_shock", _shock_defn()))["redemption"]
        assert f["score"] == -2
        assert float(f["value"]) == pytest.approx(-0.6, abs=0.01)

    def test_mild_dip_scores_minus_one(self):
        """近 5 日 −0.3%：局部承压，但还没到负反馈。"""
        f = _by_key(self._eng(_flat(200, 1000.0) + [997.0] * 5)
                    .compute("t_shock", _shock_defn()))["redemption"]
        assert f["score"] == -1

    def test_insufficient_history_is_none(self):
        f = _by_key(self._eng([1000.0, 1000.0, 1000.0])
                    .compute("t_shock", _shock_defn()))["redemption"]
        assert f["score"] is None
        assert "历史不足" in f["detail"]

    def test_asof_follows_latest_trade_date(self):
        f = _by_key(self._eng(_flat(200, 1000.0))
                    .compute("t_shock", _shock_defn()))["redemption"]
        assert f["asof"] == END.isoformat()


class TestResearchBlocks:
    """研究表达层：情景推演 / 阶段定位。二者都不参与打分，只回答「当前落在哪一格」。"""

    def _defn(self):
        return {
            "id": "t_blk", "name": "T", "version": "1",
            "subject": {"index_code": "X", "benchmark_code": "X"},
            "data": {"cn10_y": {"kind": "macro", "code": "cn10_y", "periods": 400}},
            "factors": [{"key": "c", "compute": "rate_level_trend", "name": "c",
                         "series": "cn10_y", "metric": "cn10_y", "unit": "%",
                         "thresholds_key": "th", "higher_is_better": False}],
            "thresholds": {"th": [[-99, 1.70, 1], [1.70, 99, -1]]},
            "scenarios": [
                {"name": "低利率情景", "probability": 60, "outcome": "利好",
                 "when": [{"metric": "cn10_y", "op": "<", "value": 1.70}]},
                {"name": "高利率情景", "probability": 40, "outcome": "承压",
                 "when": [{"metric": "cn10_y", "op": ">=", "value": 1.70}]},
            ],
            "phases": [
                {"name": "低利率阶段", "from": "2020-01", "to": "2029-12", "feature": "x"},
                {"name": "未来阶段", "from": "2030-01", "to": "", "feature": "y"},
            ],
        }

    def _eng(self):
        return _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                            _series(_flat(60, 1.45)))

    def test_scenario_hit_requires_all_conditions_and_exposes_current(self):
        res = self._eng().compute("t_blk", self._defn())
        sc = {s["name"]: s for s in res["scenarios"]}
        assert sc["低利率情景"]["hit"] is True
        assert sc["高利率情景"]["hit"] is False
        # 逐条带出当前值，前端才能显示「差哪一条」而不只是「没命中」
        cond = sc["低利率情景"]["conditions"][0]
        assert cond["hit"] is True and cond["current"] == "1.6862%"

    def test_phase_marks_current_by_data_asof(self):
        res = self._eng().compute("t_blk", self._defn())
        ph = {p["name"]: p for p in res["phases"]}
        assert ph["低利率阶段"]["current"] is True      # data_asof 落在 2020-01~2029-12
        assert ph["未来阶段"]["current"] is False

    def test_blocks_absent_is_empty_not_error(self):
        """没有声明这些块的台账不该报错（三个台账里只有部分声明了）。"""
        res = self._eng().compute("t_blk", _fx_defn())
        assert res["scenarios"] == [] and res["phases"] == [] and res["transmission"] == []


class TestVolumePercentile:
    """成交额分位（拥挤度的「资金维度」代理）。

    方向：报告说「A 股成交额与红利策略负相关——成交疲软时红利相对占优」，
    所以**分位越高判越不利**（越拥挤）。这条方向不能凭直觉反过来写。
    """

    def _defn(self):
        return {
            "id": "t_vol", "name": "T", "version": "1",
            "subject": {"index_code": "X", "benchmark_code": "X"},
            "data": {"idx": {"kind": "index_daily", "code": "H30269", "days": 900}},
            "factors": [{"key": "flow", "compute": "volume_percentile", "name": "资金活跃度",
                         "frame": "idx", "metric": "vol_pct", "unit": "分位",
                         "thresholds_key": "vp", "volume_ma_days": 20}],
            "thresholds": {"vp": [[-99, 20, 1], [20, 70, 0], [70, 90, -1], [90, 999, -2]]},
        }

    def _eng(self, amounts):
        n = len(amounts)
        return StubEngine(cn10=_series(_flat(60, 1.6862)),
                          red=_index_df(_flat(n, 1000.0), amounts=amounts),
                          base=_index_df(_flat(n, 1000.0)))

    def test_rising_turnover_scores_negative(self):
        """成交额单调抬升 → 当前 20 日均额是历史最高 → 分位≈100 → −2（拥挤）。"""
        f = _by_key(self._eng(list(range(1, 201))).compute("t_vol", self._defn()))["flow"]
        assert float(f["value"]) == pytest.approx(100.0)
        assert f["score"] == -2

    def test_falling_turnover_scores_positive(self):
        f = _by_key(self._eng(list(range(200, 0, -1))).compute("t_vol", self._defn()))["flow"]
        assert float(f["value"]) < 20
        assert f["score"] == 1

    def test_missing_amount_column_is_none_not_error(self):
        """指数没有成交额列时判数据缺失，不是抛错、也不是当成 0 分。"""
        eng = StubEngine(cn10=_series(_flat(60, 1.6862)),
                         red=_index_df(_flat(200, 1000.0)),      # 无 amount 列
                         base=_index_df(_flat(200, 1000.0)))
        f = _by_key(eng.compute("t_vol", self._defn()))["flow"]
        assert f["score"] is None and "无成交额" in f["detail"]

    def test_insufficient_history_is_none(self):
        """样本不足时判缺失——2 个点的分位没有意义。"""
        f = _by_key(self._eng(list(range(1, 60))).compute("t_vol", self._defn()))["flow"]
        assert f["score"] is None and "历史不足" in f["detail"]


class TestManualFlag:
    """手工项在输出里必须带 manual=True：分数照常计入总分，
    但这个标记要能让前端把「这一条是人填的」标出来。"""

    def test_manual_factor_is_flagged(self):
        res = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45))).compute("t_bond", _bond_defn())
        f = _by_key(res)
        assert f["fx"]["manual"] is True
        assert f["gov_supply"]["manual"] is True

    def test_missing_manual_value_is_still_flagged(self):
        d = _bond_defn()
        d["manual"]["fx"]["usdcny"] = None
        res = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45))).compute("t_bond", d)
        assert _by_key(res)["fx"]["manual"] is True

    def test_auto_factor_is_not_flagged(self):
        res = _bond_engine(_series(_flat(60, 1.6862)), _series(_flat(60, 5.01)),
                           _series(_flat(60, 1.45))).compute("t_bond", _bond_defn())
        assert _by_key(res)["coupon"]["manual"] is False

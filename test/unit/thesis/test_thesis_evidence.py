"""研究台账「因子历史重放 + 关联性证据」纯函数单测。

全部使用**合成 ctx / 合成行情**，不联网、不读真实库：
- 重放（无前视、采样上限、指纹复用、异常跳过、manual 跳过）
- 频率自适应（日/周/月）与未来收益
- 五分位分层 / 当前桶 / 统计量
- 滚动相关与正负相位（「什么时候反相关」是本次需求的核心）
- 方向校验（阈值表方向 vs 收益方向，冲突必须告警）
- 手工因子的快照回退（不足 30 期只说「积累中」，不画假图）
- 变动归因（今天这一分是谁给的）
- 服务层编排（get_all_factor_evidence 的 details 字段 / get_report_data 组装与降级）
"""
import numpy as np
import pytest

from thesis import evidence as ev

# ── 合成数据工具 ──────────────────────────────────────────────────────


def _bdates(n: int, start: str = "2024-01-01") -> list[str]:
    """n 个工作日日期（升序）。"""
    from datetime import date, timedelta

    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _rows(dates, vals) -> list[dict]:
    return [{"report_date": d, "value": v} for d, v in zip(dates, vals)]


def _frame_ctx(dates, closes, series=None) -> dict:
    import pandas as pd

    frames = {"px": pd.DataFrame({"trade_date": dates, "close": closes})}
    return {"series": dict(series or {}), "frames": frames, "stale_days": {}}


DEFN = {
    "id": "t", "name": "测试台账",
    "subject": {"index_code": "H30269", "index_name": "中证红利低波动"},
    "evidence": {"target": {"kind": "frame", "alias": "px",
                            "name": "中证红利低波动", "unit": "点"}},
    "thresholds": {"up": [[-99, 0, -1], [0, 99, 1]],
                   "down": [[-99, 0, 1], [0, 99, -1]]},
}


def _computer_last(engine, spec, ctx, defn, params, thresholds, warnings):
    """最小计算器：返回截断后序列的最后一个读数（验证截断/无前视）。"""
    rows = (ctx.get("series") or {}).get(spec["series"]) or []
    if not rows:
        return {"value": None, "score": None}, {}
    return {"value": rows[-1]["value"], "score": None}, {}


class FakeEngine:
    """只有注册表的最小引擎桩（不碰网络/库）。"""

    def __init__(self):
        self.calls = 0

        def counted(engine, spec, ctx, defn, params, thresholds, warnings):
            self.calls += 1
            return _computer_last(engine, spec, ctx, defn, params, thresholds, warnings)

        def boom(engine, spec, ctx, defn, params, thresholds, warnings):
            raise RuntimeError("calc failed")

        self.FACTOR_COMPUTERS = {"level": counted, "boom": boom,
                                 "last": _computer_last}


def _spec(key="a", compute="level", series="x", thresholds_key="up", unit=""):
    return {"key": key, "name": "① 测试因子", "compute": compute, "series": series,
            "thresholds_key": thresholds_key, "unit": unit}


def _target(ctx, defn=None):
    return ev.resolve_target(defn or DEFN, ctx)


def _price_from_returns(rets, base=100.0):
    px = [base]
    for r in rets[:-1]:
        px.append(px[-1] * (1 + r / 100.0))
    return px


# ── 频率判定 ──────────────────────────────────────────────────────────

class TestFreq:
    def test_weekday_series_is_daily(self):
        assert ev.detect_freq(_bdates(40)) == "daily"

    def test_weekly_gaps(self):
        d = _bdates(40)
        assert ev.detect_freq(d[::5]) == "weekly"

    def test_monthly_gaps(self):
        dates = ["2023-%02d-28" % m for m in range(1, 13)]
        assert ev.detect_freq(dates) == "monthly"

    def test_horizons_scale_with_freq(self):
        assert [h for h, _ in ev.freq_meta("daily")["horizons"]] == [20, 60]
        assert [h for h, _ in ev.freq_meta("weekly")["horizons"]] == [4, 12]
        assert [h for h, _ in ev.freq_meta("monthly")["horizons"]] == [1, 3]
        assert ev.freq_meta(None) is ev.freq_meta("daily")

    def test_short_series_defaults_daily(self):
        assert ev.detect_freq([]) == "daily"
        assert ev.detect_freq(["2024-01-01"]) == "daily"


# ── 目标标的解析 ──────────────────────────────────────────────────────

class TestResolveTarget:
    def test_frame_target_from_definition(self):
        dates = _bdates(50)
        ctx = _frame_ctx(dates, [100 + i for i in range(50)])
        tgt = _target(ctx)
        assert tgt["kind"] == "frame" and tgt["alias"] == "px"
        assert tgt["code"] == "H30269"          # code 取自 subject
        assert tgt["n"] == 50 and tgt["freq"] == "daily"
        assert tgt["freq_label"] == "日频"
        assert tgt["values"][-1] == 149

    def test_series_target_monthly(self):
        dates = ["2023-%02d-28" % m for m in range(1, 13)]
        defn = {"subject": {}, "evidence": {"target": {
            "kind": "series", "alias": "au", "name": "上海金", "unit": "元/克"}}}
        ctx = {"series": {"au": _rows(dates, [500 + i for i in range(12)])},
               "frames": {}, "stale_days": {}}
        tgt = ev.resolve_target(defn, ctx)
        assert tgt["kind"] == "series" and tgt["freq"] == "monthly"
        assert tgt["n"] == 12 and tgt["unit"] == "元/克"

    def test_fallback_to_service_mapping_uses_index_daily(self):
        """定义没有 evidence.target 时，回退到服务层映射（老台账兼容）。"""
        import pandas as pd

        dates = _bdates(40)
        df = pd.DataFrame({"trade_date": dates, "close": [10.0 + i for i in range(40)]})
        calls = []

        def loader(code):
            calls.append(code)
            return df

        defn = {"subject": {"index_code": "", "index_name": ""}}
        tgt = ev.resolve_target(defn, {"series": {}, "frames": {}},
                                fallback=("H30269", "红利低波"), index_loader=loader)
        assert calls == ["H30269"]
        assert tgt["code"] == "H30269" and tgt["name"] == "红利低波"
        assert tgt["freq"] == "daily"

    def test_fallback_to_subject_index_code(self):
        import pandas as pd

        dates = _bdates(30)
        df = pd.DataFrame({"trade_date": dates, "close": [1.0] * 30})
        tgt = ev.resolve_target({"subject": {"index_code": "000300"}},
                                {}, index_loader=lambda code: df)
        assert tgt["code"] == "000300"

    def test_none_when_nothing_available(self):
        assert ev.resolve_target({"subject": {}}, {}, index_loader=lambda c: None) is None

    def test_broken_series_target_falls_back(self):
        """声明的 alias 没数据（取数失败）→ 继续走回退，而不是直接放弃。"""
        import pandas as pd

        dates = _bdates(30)
        df = pd.DataFrame({"trade_date": dates, "close": [5.0] * 30})
        defn = {"subject": {"index_code": "000905"},
                "evidence": {"target": {"kind": "series", "alias": "nope"}}}
        tgt = ev.resolve_target(defn, {"series": {}, "frames": {}},
                                index_loader=lambda c: df)
        assert tgt["code"] == "000905"


# ── 历史重放（无前视） ────────────────────────────────────────────────

class TestReplay:
    def _ctx(self, n=120):
        dates = _bdates(n)
        vals = [float(i) for i in range(n)]
        return dates, {"series": {"x": _rows(dates, vals)},
                       "frames": {}, "stale_days": {}}

    def test_replay_has_no_lookahead(self):
        """每个采样日的读数必须是「≤d 的最后一个值」，不能取到后面的数据。"""
        dates, ctx = self._ctx(60)
        got = ev.replay_factor_series(FakeEngine(), _spec(), ctx, DEFN, {}, dates)
        assert [r["date"] for r in got] == dates
        assert [r["value"] for r in got] == [float(i) for i in range(60)]

    def test_manual_compute_is_skipped(self):
        dates, ctx = self._ctx(40)
        spec = _spec(compute="manual", series=None)
        assert ev.replay_factor_series(FakeEngine(), spec, ctx, DEFN, {}, dates) == []

    def test_unregistered_compute_returns_empty(self):
        dates, ctx = self._ctx(40)
        spec = _spec(compute="not_exist")
        assert ev.replay_factor_series(FakeEngine(), spec, ctx, DEFN, {}, dates) == []

    def test_calculator_exception_is_skipped_not_fatal(self):
        dates, ctx = self._ctx(40)
        got = ev.replay_factor_series(FakeEngine(), _spec(compute="boom"),
                                      ctx, DEFN, {}, dates)
        assert got == []

    def test_none_value_is_skipped(self):
        dates, ctx = self._ctx(10)
        ctx["series"]["x"] = _rows(dates[5:], [5.0, None, 7.0, 8.0, 9.0])
        got = ev.replay_factor_series(FakeEngine(), _spec(), ctx, DEFN, {}, dates)
        assert all(r["value"] is not None for r in got)
        assert len(got) < len(dates)

    def test_sample_axis_capped_and_keeps_last_day(self):
        dates, ctx = self._ctx(500)
        got = ev.replay_factor_series(FakeEngine(), _spec(), ctx, DEFN, {},
                                      dates, max_points=240)
        assert len(got) <= 240
        assert got[-1]["date"] == dates[-1]
        assert got[0]["date"] == dates[0]

    def test_unchanged_data_reuses_previous_value(self):
        """月频数据跑在日频轴上：相邻采样日的指纹相同，不该重复调计算器。"""
        dates = _bdates(200)
        # 只有 10 个月末读数（间隔约 20 个工作日）
        months = dates[::20]
        ctx = {"series": {"x": _rows(months, [float(i) for i in range(len(months))])},
               "frames": {}, "stale_days": {}}
        engine = FakeEngine()
        got = ev.replay_factor_series(engine, _spec(), ctx, DEFN, {}, dates)
        assert got, "年内多月读数必须能重放出序列"
        # 10 段数据 → 计算次数应远小于 200 个采样日
        assert engine.calls <= len(months) + 2

    def test_frame_ctx_is_truncated_too(self):
        """K 线口径的因子（crowding 等）同样不许看到 d 之后的行。"""
        dates = _bdates(30)
        ctx = _frame_ctx(dates, [100 + i for i in range(30)])

        def last_close(engine, spec, ctx, defn, params, thresholds, warnings):
            df = ctx["frames"]["px"]
            return {"value": float(df["close"].tolist()[-1]), "score": None}, {}

        engine = FakeEngine()
        engine.FACTOR_COMPUTERS["close"] = last_close
        got = ev.replay_factor_series(engine, _spec(compute="close", series="px"),
                                      ctx, DEFN, {}, dates)
        assert got[-1]["value"] == 129.0
        assert got[10]["value"] == 110.0

    def test_scores_follow_threshold_table(self):
        dates = _bdates(40)
        vals = [float(i - 20) for i in range(40)]      # −20 … +19
        ctx = {"series": {"x": _rows(dates, vals)}, "frames": {}, "stale_days": {}}
        got = ev.replay_factor_series(FakeEngine(), _spec(), ctx, DEFN, {}, dates)
        assert got[0]["value"] == -20 and got[0]["score"] == -1
        assert got[20]["value"] == 0 and got[20]["score"] == 1


class TestFactorAxis:
    """重放/统计的采样轴是**因子输入数据**的日期轴，不是目标标的的日期轴。

    真实案例：dividend_env 目标 521 个交易日，而指数股息率只有近 49 个交易日；
    若按目标轴每 3 天采一个，premium 只剩 15 个点（<30）→ 被误判「样本不足」。
    """

    def test_replay_axis_keeps_sparse_factor_points(self):
        tgt_dates = _bdates(300)
        ctx = _frame_ctx(tgt_dates, [100 + i * 0.1 for i in range(300)])
        fac_dates = tgt_dates[-45:]                      # 因子只有近 45 个观测
        ctx["series"]["x"] = _rows(fac_dates, [float(i) for i in range(45)])
        got = ev.replay_factor_series(FakeEngine(), _spec(), ctx, DEFN, {}, tgt_dates)
        assert [r["date"] for r in got] == fac_dates     # 一个点都没被采样漏掉
        assert len(got) == 45

    def test_sparse_factor_has_its_own_sample_count(self):
        """n 必须是因子观测数，不能被 ffill 成目标日数（441 那种伪独立样本）。"""
        n_tgt, n_fac = 300, 60
        tgt_dates = _bdates(n_tgt)
        closes = _price_from_returns([0.1 + 0.01 * i for i in range(n_tgt)])
        ctx = _frame_ctx(tgt_dates, closes)
        fac_dates = tgt_dates[-n_fac:]
        ctx["series"]["x"] = _rows(fac_dates, [float(i) for i in range(n_fac)])
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        assert out["supported"] is True
        assert out["sample"]["n"] == n_fac
        # 有效样本 = 至少有未来收益可算的那部分（尾部 h 期不算）
        assert out["sample"]["effective_n"] <= n_fac
        assert out["sample"]["effective_n"] >= n_fac - 30
        assert out["sample"]["note"]
        # horizons n ≈ 因子观测数（尾部少 h 个，因为未来收益还没走完）
        h0 = out["horizons"][0]
        assert n_fac - 30 <= h0["n"] <= n_fac
        assert out["rolling"]["dates"][0] == fac_dates[0]
        assert len(out["rolling"]["dates"]) <= n_fac

    def test_output_thinning_does_not_cap_replay_axis(self):
        """输出抽稀（240 点）不能顺带把重放轴也砍了——那是 premium 只剩 26 点的根因。"""
        n = 400
        dates = _bdates(n)
        closes = _price_from_returns([0.1 + 0.005 * i for i in range(n)])
        ctx = _frame_ctx(dates, closes)
        ctx["series"]["x"] = _rows(dates, [float(i) for i in range(n)])
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        assert out["sample"]["n"] == n            # 400 个观测全部参与统计
        assert len(out["scatter"]) <= ev.MAX_POINTS
        assert len(out["rolling"]["dates"]) <= ev.MAX_POINTS

    def test_factor_axis_uses_frame_dates_for_frame_factors(self):
        """K 线口径的因子：轴取该 K 线的交易日，而不是目标（可能是另一个指数）。"""
        tgt_dates = _bdates(200)
        fac_dates = _bdates(120, "2024-03-01")
        ctx = _frame_ctx(tgt_dates, [100 + i for i in range(200)])
        import pandas as pd
        ctx["frames"]["fast"] = pd.DataFrame(
            {"trade_date": fac_dates, "close": [10.0 + i for i in range(120)]})
        spec = {"key": "a", "name": "A", "compute": "close",
                "frame": "fast", "thresholds_key": "up"}
        engine = FakeEngine()

        def last_close(engine, spec, ctx, defn, params, thresholds, warnings):
            df = ctx["frames"]["fast"]
            return {"value": float(df["close"].tolist()[-1]), "score": None}, {}

        engine.FACTOR_COMPUTERS["close"] = last_close
        got = ev.replay_factor_series(engine, spec, ctx, DEFN, {}, tgt_dates)
        assert got and got[0]["date"] == fac_dates[0]
        assert got[-1]["date"] == fac_dates[-1]


# ── 未来收益 / 频率自适应 ─────────────────────────────────────────────

class TestForwardReturns:
    def test_daily_horizon_math(self):
        closes = [100.0 * (1.01 ** i) for i in range(30)]
        fut = ev.forward_returns(closes, 20)
        assert not np.isfinite(fut[-1]) and not np.isfinite(fut[-20])
        assert fut[0] == pytest.approx((1.01 ** 20 - 1) * 100, rel=1e-6)

    def test_too_short_series_all_nan(self):
        assert not np.isfinite(ev.forward_returns([100.0, 101.0], 5)).any()


class TestWeeklyTarget:
    def test_weekly_horizons_are_4_and_12_weeks(self):
        """周频目标（如猪价）必须按 4/12 周算，不能拿 20/60 周。"""
        n = 80
        weeks = _bdates(400)[::5][:n]
        rets = [0.5 + 0.02 * i for i in range(n)]
        imports = _price_from_returns(rets)
        ctx = _frame_ctx(weeks, imports)
        ctx["series"]["x"] = _rows(weeks, list(range(n)))
        tgt = _target(ctx)
        assert tgt["freq"] == "weekly"
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=tgt)
        assert out["supported"] is True
        assert [h["h"] for h in out["horizons"]] == [4, 12]
        assert out["horizons"][0]["label"] == "未来 4 周"
        assert out["rolling"]["window"] == 12      # 周频因子观测的滚动窗口


# ── 分层 / 当前读数 ───────────────────────────────────────────────────

class TestLayers:
    def test_monotone_factor_has_positive_spread(self):
        x = np.arange(100, dtype=float)
        y = x * 0.1
        layers, idx, label = ev.quantile_layers(x, y, "pp", False, 55.0)
        assert len(layers) == 5
        assert sum(l["n"] for l in layers) == 100
        assert layers[-1]["mean_return"] > layers[0]["mean_return"]
        assert layers[-1]["win_rate"] > layers[0]["win_rate"]
        assert idx == 3 and label and label.startswith("Q3")
        assert [l["current"] for l in layers] == [False, False, True, False, False]

    def test_discrete_factor_buckets_by_value(self):
        x = np.array([-2, -1, 0, 1, 2] * 12, dtype=float)
        y = x * 2.0
        layers, idx, _ = ev.quantile_layers(x, y, "", True, 2.0)
        assert [l["lo"] for l in layers] == [-2, -1, 0, 1, 2]
        assert all(l["label"].startswith("档位") for l in layers)
        assert idx == 5 and layers[-1]["current"] is True
        assert layers[-1]["mean_return"] == 4.0

    def test_too_few_points_returns_empty(self):
        assert ev.quantile_layers([1, 2, 3], [1, 2, 3]) == ([], None, None)


class TestCurrentReading:
    def test_percentile_and_change(self):
        vals = [float(i) for i in range(40)]
        cur = ev.current_reading(vals, "pp", lag=20)
        assert cur["percentile"] == 1.0
        assert cur["min"] == 0.0 and cur["max"] == 39.0
        assert cur["change"] == 20.0
        assert cur["change_label"].startswith("较 20 期前 +20")

    def test_empty_values_returns_none(self):
        assert ev.current_reading([], "pp") is None


# ── 滚动相关与相位（核心需求） ────────────────────────────────────────

class TestRollingPhases:
    def _turning_ctx(self):
        """前半段「因子↑→收益↑」，后半段反转成「因子↑→收益↓」。"""
        n = 200
        dates = _bdates(n)
        f = [float(np.sin(i / 3.0)) for i in range(n)]
        rets = [f[i] * 0.5 if i < n // 2 else -f[i] * 0.5 for i in range(n)]
        closes = _price_from_returns(rets)
        ctx = _frame_ctx(dates, closes)
        ctx["series"]["x"] = _rows(dates, f)
        return dates, ctx

    def test_phases_flip_from_positive_to_negative(self):
        dates, ctx = self._turning_ctx()
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        roll = out["rolling"]
        vals = [v for v in roll["corr_value"] if v is not None]
        assert vals, "滚动相关不能全空"
        assert max(vals) > 0.1 and min(vals) < -0.1, "必须能看出相关性反转"
        signs = {p["sign"] for p in roll["phases"]}
        assert "positive" in signs and "negative" in signs
        assert roll["positive_share"] > 0 and roll["negative_share"] > 0
        assert roll["current_corr_value"] < 0, "末端应落在反相关区"
        assert roll["label"].startswith("滚动")

    def test_rolling_length_matches_dates(self):
        _, ctx = self._turning_ctx()
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        roll = out["rolling"]
        assert len(roll["dates"]) == len(roll["corr_value"]) == len(roll["corr_score"])

    def test_window_follows_target_freq_not_sampling_gap(self):
        """长序列被 600 点采样后采样间隔≈4 天；窗口必须仍按目标日频取 60 期。"""
        n = 3000
        dates = _bdates(n)
        closes = _price_from_returns([0.1 + 0.0005 * i for i in range(n)])
        ctx = _frame_ctx(dates, closes)
        ctx["series"]["x"] = _rows(dates, [float(i) for i in range(n)])
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        assert out["sample"]["n"] <= ev.MAX_REPLAY_POINTS
        assert out["rolling"]["window"] == 60
        assert out["rolling"]["h"] == 20

    def test_phases_group_consecutive_signs(self):
        dates = _bdates(10)
        corrs = [0.3, 0.4, 0.2, -0.1, -0.5, None, 0.5, 0.6, None, -0.2]
        phases = ev.corr_phases(dates, corrs, min_len=1)   # 关闭碎段合并，纯测分组
        got = [(p["sign"], p["n"]) for p in phases]
        assert got == [("positive", 3), ("negative", 2), ("positive", 2), ("negative", 1)]
        assert phases[0]["label"] == "正相关"
        assert phases[0]["mean_corr"] == pytest.approx(0.3, abs=1e-6)

    def test_short_phases_are_merged_into_neighbors(self):
        """一两期就翻回来的「反转」是估计噪声：默认并入相邻较长段。"""
        dates = _bdates(12)
        corrs = [0.5, 0.4, 0.5, 0.3, -0.6, 0.5, 0.6, 0.5, 0.4, -0.2, -0.8, -0.7]
        phases = ev.corr_phases(dates, corrs)
        # 中间那个孤立的负值被并入前后任一段，最终不应是 4 段
        assert len(phases) <= 3
        assert all(p["n"] >= 2 for p in phases)
        # 相位符号必须与其均值一致（否则前端会出现「正相关但均值为负」）
        for p in phases:
            assert (p["mean_corr"] >= 0) == (p["sign"] == "positive")

    def test_all_short_segments_are_not_force_merged(self):
        """全序列都碎（没有任何 ≥3 期的段）时不能硬拼成一个假相位。"""
        phases = ev.corr_phases(_bdates(4), [0.3, -0.4, 0.5, -0.6])
        assert len(phases) == 4


# ── 方向校验 ──────────────────────────────────────────────────────────

class TestDirection:
    def test_expected_sign_from_threshold_monotonicity(self):
        assert ev.expected_sign_of({"thresholds_key": "up"}, DEFN["thresholds"]) == "positive"
        assert ev.expected_sign_of({"thresholds_key": "down"}, DEFN["thresholds"]) == "negative"
        assert ev.expected_sign_of({"thresholds_key": "none"}, DEFN["thresholds"]) == "mixed"
        assert ev.expected_sign_of({"compute": "dual_axis"}, DEFN["thresholds"]) == "discrete"

    def test_aligned_when_table_direction_matches_returns(self):
        d = ev.direction_check({"thresholds_key": "up"}, DEFN["thresholds"],
                               0.42, 0.35, "未来 20 个交易日", False)
        assert d["expected_sign"] == "positive" and d["aligned"] is True
        assert "一致" in d["text"]

    def test_inverted_table_is_flagged(self):
        """阈值表写成「读数越高分越低」，但历史上读数越高收益越高 → 必须告警。"""
        d = ev.direction_check({"thresholds_key": "down"}, DEFN["thresholds"],
                               0.42, -0.35, "未来 20 个交易日", False)
        assert d["expected_sign"] == "negative"
        assert d["aligned"] is False
        assert "复核" in d["text"]

    def test_negative_corr_score_warns(self):
        """阈值表非单调时靠 corr_score 定方向：显著为负 = 评分方向反了。"""
        d = ev.direction_check({"thresholds_key": "none"}, DEFN["thresholds"],
                               -0.2, -0.4, "未来 20 个交易日", False)
        assert d["score_sign"] == "negative"
        assert d["aligned"] is False
        assert "非单调" in d["text"]

    def test_weak_corr_is_not_judged(self):
        d = ev.direction_check({"thresholds_key": "up"}, DEFN["thresholds"],
                               0.02, 0.01, "未来 20 个交易日", False)
        assert d["aligned"] is None and "不适用" in d["text"]

    def test_weak_value_corr_is_not_judged_even_if_score_strong(self):
        """lead 案例：hog rank r=-0.08 / cu rank r=+0.04 不许报「评分方向可能反了」。"""
        d = ev.direction_check({"thresholds_key": "up"}, DEFN["thresholds"],
                               -0.08, -0.40, "未来 20 个交易日", False)
        assert d["aligned"] is None
        assert "不适用" in d["text"] and "反了" not in d["text"]

    def test_weak_value_corr_on_non_monotone_table(self):
        d = ev.direction_check({"thresholds_key": "none"}, DEFN["thresholds"],
                               0.04, 0.30, "未来 20 个交易日", False)
        assert d["aligned"] is None

    def test_discrete_factor_direction(self):
        d = ev.direction_check({"compute": "dual_axis"}, DEFN["thresholds"],
                               0.3, 0.3, "未来 20 个交易日", True)
        assert d["expected_sign"] == "discrete"
        assert "离散" in d["text"]


# ── 端到端：build_evidence ────────────────────────────────────────────

class _PositiveCtx:
    """因子 = 递增序号；收益递增 → 强正相关、分位单调。"""

    def __init__(self, n=120):
        self.n = n
        self.dates = _bdates(n)
        rets = [0.1 + 0.01 * i for i in range(n)]
        self.closes = _price_from_returns(rets)
        self.ctx = _frame_ctx(self.dates, self.closes)
        self.ctx["series"]["x"] = _rows(self.dates, [float(i) for i in range(n)])


class TestBuildEvidence:
    def test_full_contract_fields(self):
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        for key in ("supported", "factor_key", "factor_name", "unit", "discrete",
                    "source", "target", "sample", "current", "horizons", "rolling",
                    "scatter", "scatter_score", "dual", "direction", "verdict",
                    "caveats", "reason", "series_start"):
            assert key in out, key
        assert out["supported"] is True and out["source"] == "replay"
        assert out["reason"] is None
        assert out["target"]["code"] == "H30269"
        assert out["target"]["freq"] == "daily"
        assert out["sample"]["n"] == p.n and out["sample"]["start"] == p.dates[0]
        assert out["current"]["value"] == p.n - 1
        assert out["current"]["percentile"] == 1.0
        assert out["current"]["bucket_code"] == "Q5"
        assert out["current"]["bucket_mean_return"] is not None
        assert out["verdict"] and "正" in out["verdict"]
        assert out["caveats"] and any("重叠" in c for c in out["caveats"])
        # 旧字段兼容（页面/旧测试仍在读）
        assert out["horizons"][0]["corr"] == out["horizons"][0]["corr_value"]
        assert len(out["horizons"]) == 2
        assert out["scatter"] and out["scatter_score"]
        assert set(out["dual"]) == {"dates", "factor", "target",
                                    "factor_norm", "target_norm",
                                    "method", "label"}
        assert out["dual"]["method"] == "zscore_x10_base100"
        assert all(60 <= v <= 140 for v in out["dual"]["factor_norm"])

    def test_horizon_stats_shape(self):
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        h = out["horizons"][0]
        assert h["h"] == 20 and h["n"] > 30
        assert h["corr_value"] > 0.8
        assert h["spearman_value"] > 0.8
        assert h["q5_q1"] > 0
        assert len(h["layers"]) == 5
        for layer in h["layers"]:
            for k in ("bucket", "code", "label", "lo", "hi", "mean_return",
                      "median_return", "win_rate", "n", "current"):
                assert k in layer

    def test_direction_inverted_threshold_flags(self):
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(thresholds_key="down"), ctx=p.ctx,
                                engine=FakeEngine(), params={}, target=_target(p.ctx))
        assert out["direction"]["expected_sign"] == "negative"
        assert out["direction"]["aligned"] is False

    def test_no_target_degrades(self):
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=None)
        assert out["supported"] is False
        assert "未配置目标标的" in out["reason"]
        assert out["source"] == "replay"

    def test_no_series_degrades(self):
        p = _PositiveCtx()
        p.ctx["series"]["x"] = []
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert out["supported"] is False
        assert out["series_start"] == ""

    def test_short_replay_degrades_but_keeps_current(self):
        p = _PositiveCtx(n=20)
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert out["supported"] is False
        assert "样本不足" in out["reason"] and "20" in out["reason"]
        assert out["current"]["value"] == 19

    def test_direction_sits_on_primary_horizon(self):
        """方向校验必须与滚动/散点/当前桶同口径（首个期限），不能写 60 日。"""
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert out["direction"]["h"] == out["horizons"][0]["h"] == 20
        assert "未来 20 个交易日" in out["direction"]["text"]

    def test_verdict_has_no_double_warning_glyph(self):
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(thresholds_key="down"), ctx=p.ctx,
                                engine=FakeEngine(), params={}, target=_target(p.ctx))
        assert out["direction"]["aligned"] is False
        assert "⚠ ⚠" not in out["verdict"]
        assert out["verdict"].count("⚠") == 1

    def test_weak_corr_does_not_warn_about_direction(self):
        """因子与收益基本无关时，verdict 不许出现方向告警。"""
        p = _PositiveCtx()
        p.ctx["series"]["x"] = _rows(
            p.dates, [1.0 if i % 2 else 1.5 for i in range(p.n)])
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert out["direction"]["aligned"] is None
        assert "⚠" not in out["verdict"]

    def test_short_window_added_when_regular_horizons_insufficient(self):
        """样本只够短窗口时补「未来 5 个交易日」，并明确标注 short_window。"""
        n_tgt, n_fac = 60, 45
        dates = _bdates(n_tgt)
        closes = _price_from_returns([0.2 + 0.02 * i for i in range(n_tgt)])
        ctx = _frame_ctx(dates, closes)
        # 因子只有最近 45 个观测 → 20 日前瞻只剩 25 个样本（<30），60 日一个没有
        ctx["series"]["x"] = _rows(dates[-n_fac:], [float(i) for i in range(n_fac)])
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        assert out["supported"] is True
        assert [h["h"] for h in out["horizons"]] == [20, 60, 5]
        assert out["horizons"][-1]["short_window"] is True
        assert out["horizons"][-1]["n"] >= 30
        # 代表期限自动落到短窗口（20/60 都不够 30）
        assert out["rolling"]["h"] == 5
        assert out["direction"]["h"] == 5
        assert out["direction"]["aligned"] is True     # 短窗口也能给方向校验
        assert "样本不足" not in out["verdict"]
        assert "仅作方向参考" in out["verdict"]
        assert "有实际区分度" not in out["verdict"]
        assert any("短窗口" in c for c in out["caveats"])

    def test_no_short_window_when_regular_horizon_is_enough(self):
        p = _PositiveCtx()
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert [h["h"] for h in out["horizons"]] == [20, 60]
        assert all(not h.get("short_window") for h in out["horizons"])

    def test_dual_normalization_handles_negative_factor_values(self):
        """因子读数可能为负（红利④相对收益 -14pp）：除以负基期会整体翻符号。"""
        p = _PositiveCtx()
        p.ctx["series"]["x"] = _rows(
            p.dates, [float(i - 60) for i in range(p.n)])      # −60 … +59
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        dual = out["dual"]
        assert dual["factor"][0] < 0
        # 标准化后都在 100 附近，且与读数同向（不会因为基期为负而翻转）
        assert all(60 <= v <= 140 for v in dual["factor_norm"])
        assert all(60 <= v <= 140 for v in dual["target_norm"])
        assert dual["factor_norm"][-1] > dual["factor_norm"][0]
        assert dual["target_norm"][-1] > dual["target_norm"][0]

    def test_dual_normalization_constant_series_is_flat_100(self):
        p = _PositiveCtx()
        p.ctx["series"]["x"] = _rows(p.dates, [1.0] * p.n)
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert set(out["dual"]["factor_norm"]) == {100.0}

    def test_q5_spread_withheld_when_samples_insufficient(self):
        """n<30 时不给 Q5−Q1（20 来个样本的分层差会被当成显著结论）。"""
        n_tgt, n_fac = 60, 45
        dates = _bdates(n_tgt)
        closes = _price_from_returns([0.2 + 0.02 * i for i in range(n_tgt)])
        ctx = _frame_ctx(dates, closes)
        ctx["series"]["x"] = _rows(dates[-n_fac:], [float(i) for i in range(n_fac)])
        out = ev.build_evidence(DEFN, _spec(), ctx=ctx, engine=FakeEngine(),
                                params={}, target=_target(ctx))
        h20 = next(h for h in out["horizons"] if h["h"] == 20)
        assert h20["n"] < ev.MIN_HORIZON_SAMPLES
        assert h20["corr_value"] is None and h20["q5_q1"] is None
        assert h20["layers"], "分层明细仍要保留（各桶自带 n）"

    def test_discrete_factor_marks_discrete(self):
        p = _PositiveCtx()
        p.ctx["series"]["x"] = _rows(p.dates, [float(i % 3) for i in range(p.n)])
        spec = _spec()
        spec.pop("thresholds_key")
        out = ev.build_evidence(DEFN, spec, ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx))
        assert out["discrete"] is True
        assert out["direction"]["expected_sign"] == "discrete"


# ── 手工因子的快照回退 ────────────────────────────────────────────────

class TestSnapshotFallback:
    @staticmethod
    def _history(values, dates):
        return [{"trade_date": d, "values": {"a": v}, "scores": {"a": v},
                 "units": {"a": ""}} for d, v in zip(dates, values)]

    def test_manual_insufficient_shows_accumulating(self):
        dates = _bdates(10)
        hist = self._history(list(range(10)), dates)
        ctx = _frame_ctx(dates + _bdates(10, "2024-06-01"),
                         [100 + i for i in range(20)])
        out = ev.build_evidence(DEFN, _spec(compute="manual", series=None), ctx=ctx,
                                engine=FakeEngine(), params={},
                                target=_target(ctx),
                                snapshot_rows=ev.snapshot_series(hist, "a"))
        assert out["supported"] is False and out["source"] == "snapshot"
        assert "快照样本 10 期 < 30" in out["reason"]
        assert out["current"]["value"] == 9      # 仍给当前读数，不画假图
        assert out["sample"]["n"] == 10

    def test_manual_with_enough_snapshots_gives_evidence(self):
        n = 60
        dates = _bdates(n)
        closes = _price_from_returns([0.1 + 0.01 * i for i in range(n)])
        ctx = _frame_ctx(dates, closes)
        out = ev.build_evidence(DEFN, _spec(compute="manual", series=None), ctx=ctx,
                                engine=FakeEngine(), params={},
                                target=_target(ctx),
                                snapshot_rows=ev.snapshot_series(
                                    self._history([float(i) for i in range(n)], dates), "a"))
        assert out["supported"] is True and out["source"] == "snapshot"
        assert out["horizons"][0]["corr_value"] > 0.5
        assert "快照" in out["caveats"][0]

    def test_manual_without_snapshots_is_explicit(self):
        out = ev.build_evidence(DEFN, _spec(compute="manual"), ctx={},
                                engine=FakeEngine(), params={}, target=None)
        assert out["supported"] is False and "手工维护因子" in out["reason"]

    def test_short_replay_does_not_swap_in_longer_snapshot(self):
        """回放只出 20 期时不能拿 45 期快照顶替：两套口径混用会让「期数」失真。"""
        p = _PositiveCtx(n=20)
        dates = _bdates(45, "2023-06-01")
        hist = self._history([float(i) for i in range(45)], dates)
        out = ev.build_evidence(DEFN, _spec(), ctx=p.ctx, engine=FakeEngine(),
                                params={}, target=_target(p.ctx),
                                snapshot_rows=ev.snapshot_series(hist, "a"))
        assert out["supported"] is False
        assert out["source"] == "replay"
        assert "因子序列样本不足（20 期 < 30）" in out["reason"]


# ── 变动归因 ──────────────────────────────────────────────────────────

class TestAttribution:
    def _items(self):
        return [
            {"trade_date": "2026-10-24", "total": 3,
             "scores": {"a": 1, "b": 0, "c": 2},
             "values": {"a": 10.0, "b": 5.0, "c": 1.0},
             "units": {"a": "pp", "b": "", "c": "%"}},
            {"trade_date": "2026-10-27", "total": 4,
             "scores": {"a": 2, "b": 0, "c": 1},
             "values": {"a": 12.0, "b": 5.0, "c": 0.5},
             "units": {"a": "pp", "b": "", "c": "%"}},
        ]

    def test_rows_sorted_and_delta(self):
        out = ev.build_attribution(self._items(),
                                   {"a": "① 股息率溢价", "b": "② 利率", "c": "③ 拥挤度"})
        assert out["prev_trade_date"] == "2026-10-24"
        assert out["cur_trade_date"] == "2026-10-27"
        assert out["total_delta"] == 1
        assert [r["key"] for r in out["rows"]][:2] == ["a", "c"]
        assert out["rows"][0]["delta"] == 1
        assert out["rows"][0]["value_delta"] == 2.0
        assert out["rows"][0]["direction"] == "up"
        assert out["rows"][1]["direction"] == "down"
        assert out["rows"][0]["name"] == "① 股息率溢价"

    def test_note_is_human_readable(self):
        out = ev.build_attribution(self._items(),
                                   {"a": "① 股息率溢价", "b": "② 利率", "c": "③ 拥挤度"})
        assert "总分 +3 → +4（+1）" in out["note"]
        assert "① +1" in out["note"] and "③ -1" in out["note"]
        assert "1 项不变" in out["note"]

    def test_single_point_is_placeholder(self):
        out = ev.build_attribution(self._items()[:1])
        assert out["rows"] == [] and out["total_delta"] is None
        assert "不足 2 个" in out["note"]

    def test_missing_score_yields_none_delta(self):
        items = self._items()
        items[1]["scores"] = {"a": None, "b": 0, "c": 1}
        out = ev.build_attribution(items)
        row = next(r for r in out["rows"] if r["key"] == "a")
        assert row["delta"] is None and row["direction"] is None


# ── 服务层编排（details / report 组装与降级） ─────────────────────────

def _rich_evidence():
    return {
        "supported": True, "factor_name": "① 测试因子",
        "current": {"value": 2.55, "percentile": 0.78, "bucket_code": "Q4",
                    "bucket_mean_return": 0.8, "bucket_win_rate": 56.0,
                    "bucket_n": 42},
        "rolling": {"current_corr_value": -0.18, "positive_share": 62.0,
                    "dates": ["2024-01-02"], "corr_value": [0.31]},
        "direction": {"expected_sign": "positive", "aligned": True},
        "horizons": [{"h": 20, "corr": 0.21, "corr_score": 0.18, "q5_q1": 1.3,
                      "n": 210, "layers": [{"bucket": 1, "code": "Q1"}]}],
        "verdict": "v", "unit": "pp", "discrete": False, "source": "replay",
    }


class TestServiceOrchestration:
    def test_best_horizon_prefers_regular_over_short_window(self):
        """短窗口 r 天生偏大，不能让它决定「因子强不强」的档位。"""
        from server.thesis_service import _best_horizon
        ev_ = {"horizons": [{"h": 20, "corr": 0.2},
                            {"h": 60, "corr": 0.1},
                            {"h": 5, "corr": 0.9, "short_window": True}]}
        assert _best_horizon(ev_)["h"] == 20

    def test_best_horizon_falls_back_to_short_window(self):
        from server.thesis_service import _best_horizon
        ev_ = {"horizons": [{"h": 20, "n": 10},
                            {"h": 5, "corr": 0.4, "short_window": True}]}
        assert _best_horizon(ev_)["h"] == 5

    def test_short_window_tier_is_capped_at_weak(self):
        """短窗口 r 再大也不能把「刚积累一个月」的因子评成强支撑。"""
        from server.thesis_service import _evidence_tier
        ev_ = {"supported": True,
               "horizons": [{"h": 20, "n": 10},
                            {"h": 5, "corr": 0.62, "q5_q1": 3.0,
                             "short_window": True}]}
        assert _evidence_tier(ev_) == "weak"

    def test_batch_row_n_falls_back_to_primary_horizon(self, monkeypatch):
        """corr 全为 null 时行内 n/h 要回落到首个期限，report 表不能整列空白。"""
        from server import thesis_service as svc
        ev_ = {"supported": True, "factor_name": "① x",
               "horizons": [{"h": 20, "n": 24, "corr": None},
                            {"h": 60, "n": 0, "corr": None}],
               "current": {"value": 1.0}, "reason": None}
        monkeypatch.setattr(svc, "_evidence_prep", lambda tid: {
            "defn": {"name": "T", "factors": [{"key": "a", "name": "A"}]},
            "engine": None, "params": {}, "ctx": {}, "target": {}, "history": []})
        monkeypatch.setattr(svc, "_compute_factor_evidence",
                            lambda tid, key, prep=None: ev_)
        row = svc.get_all_factor_evidence("t1")["rows"][0]
        assert row["n"] == 24 and row["h"] == 20
        assert row["corr"] is None and row["q5_q1"] is None

    def test_batch_details_fields(self, monkeypatch):
        from server import thesis_service as svc
        monkeypatch.setattr(svc, "_evidence_prep", lambda tid: {
            "defn": {"name": "T", "factors": [{"key": "a", "name": "A"}]},
            "engine": None, "params": {}, "ctx": {}, "target": {}, "history": []})
        monkeypatch.setattr(svc, "_compute_factor_evidence",
                            lambda tid, key, prep=None: _rich_evidence())
        out = svc.get_all_factor_evidence("t1", details=True)
        row = out["rows"][0]
        assert row["current_value"] == 2.55 and row["percentile"] == 0.78
        assert row["current_bucket"] == "Q4"
        assert row["bucket_mean_return"] == 0.8 and row["bucket_win_rate"] == 56.0
        # 胜率的分母必须是当前桶 n；全样本 n 是另一列，不能混用
        assert row["bucket_n"] == 42 and row["n"] == 210
        assert row["rolling_current"] == -0.18 and row["rolling_positive_share"] == 62.0
        assert row["direction"] == "positive" and row["aligned"] is True
        assert row["rolling"]["corr_value"] == [0.31]
        assert row["layers"][0]["code"] == "Q1"

    def test_batch_without_details_omits_heavy_fields(self, monkeypatch):
        from server import thesis_service as svc
        monkeypatch.setattr(svc, "_evidence_prep", lambda tid: {
            "defn": {"name": "T", "factors": [{"key": "a", "name": "A"}]},
            "engine": None, "params": {}, "ctx": {}, "target": {}, "history": []})
        monkeypatch.setattr(svc, "_compute_factor_evidence",
                            lambda tid, key, prep=None: _rich_evidence())
        row = svc.get_all_factor_evidence("t1")["rows"][0]
        assert "rolling" not in row and "layers" not in row
        assert row["corr"] == 0.21 and row["corr_score"] == 0.18

    def test_batch_isolates_factor_failure(self, monkeypatch):
        from server import thesis_service as svc
        monkeypatch.setattr(svc, "_evidence_prep", lambda tid: {
            "defn": {"name": "T", "factors": [{"key": "a", "name": "A"}]},
            "engine": None, "params": {}, "ctx": {}, "target": {}, "history": []})

        def boom(tid, key, prep=None):
            raise RuntimeError("算炸了")

        monkeypatch.setattr(svc, "_compute_factor_evidence", boom)
        out = svc.get_all_factor_evidence("t1")
        assert out["ok"] is True and out["rows"][0]["supported"] is False
        assert "算炸了" in out["rows"][0]["reason"]

    # ── report 数据组装 ──

    @staticmethod
    def _store(tmp_path):
        from thesis.storage import ThesisStorage
        ThesisStorage._instance = None
        store = ThesisStorage(db_path=tmp_path / "thesis.db")
        return store

    @staticmethod
    def _result(total, scores, values, trade_date):
        return {
            "thesis_id": "dividend_env", "name": "红利环境因子包",
            "asset_label": "中证红利低波动", "data_asof": trade_date,
            "total": total, "valid_count": 2, "factor_count": 2,
            "verdict": "中性", "action": "观望",
            "factors": [
                {"key": "premium", "name": "① 股息率溢价", "value": values[0],
                 "unit": "pp", "score": scores[0], "status": "中性", "asof": trade_date},
                {"key": "style_rel", "name": "④ 成长风格相对强弱", "value": values[1],
                 "unit": "pp", "score": scores[1], "status": "有利", "asof": trade_date},
            ],
            "signals": {"buy": [], "warn": []}, "invalidation": [],
            "warnings": [], "metric_values": {},
        }

    def test_report_data_shape_and_attribution(self, tmp_path, monkeypatch):
        from server import thesis_service as svc
        store = self._store(tmp_path)
        try:
            store.save_snapshot(self._result(3, [1, 1], [10.0, 15.0], "2026-10-24"),
                                trade_date="2026-10-24")
            store.save_snapshot(self._result(4, [2, 0], [12.0, 17.0], "2026-10-27"),
                                trade_date="2026-10-27")
            monkeypatch.setattr(svc, "get_all_factor_evidence",
                                lambda tid, details=False: {"ok": True, "rows": []})
            out = svc.get_report_data("dividend_env", storage=store)
        finally:
            from thesis.storage import ThesisStorage
            ThesisStorage._instance = None

        for key in ("generated_at", "thesis", "snapshot", "history",
                    "definition", "attribution", "evidence", "alerts"):
            assert key in out, key
        assert out["thesis"]["id"] == "dividend_env"
        assert out["history"]["count"] == 2
        assert out["attribution"]["total_delta"] == 1
        assert "总分 +3 → +4（+1）" in out["attribution"]["note"]
        assert out["evidence"]["ok"] is True
        keys = [f["key"] for f in out["definition"]["factors"]]
        # 定义来自真实 data/thesis/dividend_env.json（不联网），因子表原样透出
        assert "premium" in keys and "style_rel" in keys

    def test_report_cache_hit_force_and_invalidate(self, tmp_path, monkeypatch):
        """报告组装 30 分钟内复用；force 绕过；显式失效后重算。

        用户同一张台账「生成 → 看 → 调格式 → 再导出」不应每次重付 engine._prepare
        的 5~70s；但点「刷新」/强制重新生成必须能拿到新数据。
        """
        from server import thesis_service as svc
        store = self._store(tmp_path)
        calls = {"n": 0}

        def fake_batch(tid, details=False):
            calls["n"] += 1
            return {"ok": True, "rows": [], "call": calls["n"]}

        try:
            monkeypatch.setattr(svc, "get_all_factor_evidence", fake_batch)
            first = svc.get_report_data("dividend_env", storage=store)
            second = svc.get_report_data("dividend_env", storage=store)
            assert calls["n"] == 1
            assert first["cache"]["hit"] is False
            assert second["cache"]["hit"] is True
            assert second["cache"]["ttl_s"] == svc.REPORT_CACHE_TTL_S

            forced = svc.get_report_data("dividend_env", storage=store, force=True)
            assert calls["n"] == 2
            assert forced["cache"]["hit"] is False

            svc.invalidate_report_cache("dividend_env")
            svc.get_report_data("dividend_env", storage=store)
            assert calls["n"] == 3
        finally:
            from thesis.storage import ThesisStorage
            ThesisStorage._instance = None
            svc.invalidate_report_cache("dividend_env")

    def test_report_survives_evidence_failure(self, tmp_path, monkeypatch):
        from server import thesis_service as svc
        store = self._store(tmp_path)
        try:
            store.save_snapshot(self._result(3, [1, 1], [10.0, 15.0], "2026-10-24"),
                                trade_date="2026-10-24")
            monkeypatch.setattr(svc, "get_all_factor_evidence",
                                lambda tid, details=False: (_ for _ in ()).throw(
                                    RuntimeError("取数挂了")))
            out = svc.get_report_data("dividend_env", storage=store)
        finally:
            from thesis.storage import ThesisStorage
            ThesisStorage._instance = None
        assert out["evidence"]["ok"] is False
        assert "取数挂了" in out["evidence"]["reason"]
        assert out["snapshot"]["total"] == 3      # 其余区块照常给

    def test_report_unknown_thesis_raises(self, tmp_path):
        from server import thesis_service as svc
        store = self._store(tmp_path)
        try:
            with pytest.raises(ValueError, match="台账定义不存在"):
                svc.get_report_data("no_such_thesis", storage=store)
        finally:
            from thesis.storage import ThesisStorage
            ThesisStorage._instance = None

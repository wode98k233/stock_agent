"""co_exposure 对比回测脚本的回归测试。

重点不是「结果好不好看」，而是**方法学正确性**——回测最容易出的错是：
① 前视偏差（用未来信息决定当前仓位）；
② 杠杆/仓位错误（未触发时写成 200% 这类）；
③ 把「资产选择」误当「择时能力」（缺对照组）。
本文件锁死前两条，第三条由脚本里的 always_defense/always_offense 对照覆盖。
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bt_coexp",
    str(Path(__file__).resolve().parents[3] / "scripts" / "devtools" / "backtest_co_exposure.py"))
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


class TestWeakFlagsNoLookahead:
    """弱势标记只能用 ≤ t 的数据。"""

    @pytest.mark.parametrize("rule,lookback", [("trend_neg", 20), ("below_ma", 250),
                                                ("mom_and_ma", 60)])
    def test_first_lookback_entries_are_false(self, rule, lookback):
        closes = [100 + i * 0.5 for i in range(400)]
        flags = bt.weak_flags(closes, rule, lookback)
        assert len(flags) == len(closes)
        assert not any(flags[:lookback]), "前 lookback 期样本不足，必须判 False（不得用未来数据凑）"

    def test_monotone_rising_series_is_never_weak(self):
        closes = [100 * (1.001 ** i) for i in range(400)]
        for rule, lb in (("trend_neg", 20), ("below_ma", 250), ("mom_and_ma", 60)):
            assert not any(bt.weak_flags(closes, rule, lb)), rule

    def test_monotone_falling_series_is_weak_after_warmup(self):
        closes = [100 * (0.999 ** i) for i in range(400)]
        for rule, lb in (("trend_neg", 20), ("below_ma", 250), ("mom_and_ma", 60)):
            flags = bt.weak_flags(closes, rule, lb)
            assert flags[-1] is True, rule

    def test_unknown_rule_raises(self):
        with pytest.raises(ValueError):
            bt.weak_flags([1.0] * 50, "nope", 10)


class TestNoLeverageAndNoLookahead:
    """仓位与无前视——这两条错了整个回测就没意义。"""

    def _data(self, n=300):
        d = [0.001 if i % 3 else -0.002 for i in range(n)]
        o = [-0.003 if i % 4 else 0.004 for i in range(n)]
        return d, o

    @pytest.mark.parametrize("kind,params", [
        ("base", None), ("rotate", None),
        ("derisk", {"gross": 0.5}), ("derisk", {"gross": 0.3}),
        ("always_defense", None), ("always_offense", None),
    ])
    def test_exposure_never_exceeds_full(self, kind, params):
        """任何策略任何一天的总仓位都不得超过 100%（曾写成 (1.0,1.0) 变 200%）。"""
        d, o = self._data()
        state = [i % 5 == 0 for i in range(len(d))]
        res = bt.run_strategy(d, o, state, kind, params)
        assert res["metrics"]["avg_exposure_pct"] <= 100.0 + 1e-6
        assert min(res["nav"]) > 0

    def test_derisk_gross_scales_exposure(self):
        """降仓幅度应线性体现在平均仓位上：70% > 50% > 30%。"""
        d, o = self._data()
        state = [i % 3 == 0 for i in range(len(d))]
        exps = [bt.run_strategy(d, o, state, "derisk", {"gross": g})["metrics"]["avg_exposure_pct"]
                for g in (0.7, 0.5, 0.3)]
        assert exps[0] > exps[1] > exps[2]

    def test_state_uses_previous_day_only(self):
        """状态只作用于**下一日**收益：nav[t+1] 由第 t 日收盘后的状态决定。

        下标约定：nav[0] 为期初；第 t 日收益写入 nav[t+1]，其权重取 state[t-1]。
        故 state[10] 应影响 nav[12] 起，且 nav[:12] 完全不变。
        """
        d, o = self._data(60)
        s = [False] * len(d)
        base_nav = bt.run_strategy(d, o, s, "rotate")["nav"]

        s2 = list(s)
        s2[10] = True                     # 第 10 日状态 → 作用于第 11 日 → 落在 nav[12]
        nav2 = bt.run_strategy(d, o, s2, "rotate")["nav"]
        assert nav2[:12] == base_nav[:12], "第 10 日状态不应影响 ≤ 第 11 日的净值（无前视）"
        assert nav2[12] != base_nav[12], "第 10 日状态应影响第 11 日净值（落在 nav[12]）"

        s3 = list(s)
        s3[30] = True                     # 更晚的状态不该影响更早的净值
        nav3 = bt.run_strategy(d, o, s3, "rotate")["nav"]
        assert nav3[:31] == base_nav[:31]

    def test_base_is_static_half_half(self):
        """基准必须与状态无关——否则「对照」就不成立。"""
        d, o = self._data(80)
        a = bt.run_strategy(d, o, [False] * len(d), "base")["nav"]
        b = bt.run_strategy(d, o, [True] * len(d), "base")["nav"]
        assert a == b

    def test_always_defense_reproduces_defense_buy_hold(self):
        """对照组的正确性：恒定满仓单腿应≈该腿买入持有（扣交易成本前）。"""
        d, o = self._data(120)
        res = bt.run_strategy(d, o, [False] * len(d), "always_defense")
        nav = 1.0
        for r in d:
            nav *= (1 + r)
        # always_defense 首次建仓无成本（prev_w=None），故应完全一致
        assert abs(res["nav"][-1] - nav) < 1e-9


class TestMetrics:
    def test_metrics_on_flat_nav(self):
        m = bt._metrics([1.0] * 10, [1.0] * 9)
        assert m["total_return_pct"] == 0.0
        assert m["max_drawdown_pct"] == 0.0

    def test_max_drawdown_detects_drop(self):
        m = bt._metrics([1.0, 1.2, 0.6, 0.8], [1.0] * 3)
        assert m["max_drawdown_pct"] == pytest.approx(-50.0, abs=0.01)

    def test_regime_corr_requires_enough_samples(self):
        d = [0.001] * 30
        o = [0.002] * 30
        state = [False] * 30
        rc = bt.regime_corr(d, o, state)
        assert rc["corr_trigger"] is None, "触发样本不足 20 应返回 None 而不是硬算"

"""K 线技术面标注（tools/ta_annotations.py）回归测试。

锁住三处已修复的问题（2026-11-07）：
1. 摆动点取 ZIGZAG 的 **trend 翻转**，不是 `zz` 非空——后者在 250 根里会取出 245 个点。
2. 支撑阻力带必须限宽，否则链式聚类会把带拉到 3 倍以上 ATR（实测 1398→1489，91 元宽）。
3. 楔形用"两轨间距变化"判定，不能只看斜率正负——下降楔形是两条都下倾、上轨更陡。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools import ta_annotations as ta


def _df(closes, highs=None, lows=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes * 1.01
    lows = np.asarray(lows, dtype=float) if lows is not None else closes * 0.99
    return pd.DataFrame({
        "date": [f"2026-01-{i % 28 + 1:02d}" for i in range(n)],
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1e6),
    })


def _zigzag_df(n=250):
    """先涨后跌的锯齿，制造明确的摆动点。"""
    x = np.arange(n)
    closes = 100 + 30 * np.sin(x / 12.0) + x * 0.1
    highs = closes + 1.5
    lows = closes - 1.5
    return _df(closes, highs, lows)


class TestZigZagPivots:
    def test_returns_flips_not_every_bar(self):
        """250 根不能返回 200+ 个摆动点（旧 bug 的症状）。"""
        df = _zigzag_df()
        piv = ta.zigzag_pivots(df)
        assert 0 < len(piv) < 60, f"摆动点 {len(piv)} 个，接近逐 bar 说明取法错了"

    def test_pivots_have_direction(self):
        piv = ta.zigzag_pivots(_zigzag_df())
        assert all(p["dir"] in (-1, 1) for p in piv)
        assert all(isinstance(p["i"], int) for p in piv)

    def test_pivot_price_is_the_real_extreme(self):
        """拐点必须落在该段的真实极值上（顶=high、底=low），不能取 ZIGZAG 的 zz。

        2026-11-13 实测 513330：旧实现取 zz[i] 得到 0.393（顶）与 0.347（底），
        而真实波段是 0.418→0.305——斐波那契被压成一半，看着不像回撤。
        """
        df = _zigzag_df()
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        for p in ta.zigzag_pivots(df):
            expect = lows[p["i"]] if p["dir"] > 0 else highs[p["i"]]
            # 输出统一保留 3 位小数，比较时放宽到 0.002
            assert p["price"] == pytest.approx(expect, abs=0.002), \
                f"拐点 {p['i']} dir={p['dir']} 价格 {p['price']} 不是该段极值 {expect}"


class TestExtendLastPivot:
    def test_low_pivot_extends_to_new_lower_low(self):
        """趋势还在创新低时，最后一个底要跟到最新极值（否则 0% 线悬在现价上方）。"""
        df = _df(np.linspace(200, 120, 40))
        pivots = [{"i": 5, "price": 150.0, "dir": 1}]
        out = ta.extend_last_pivot(df, pivots)
        assert out[-1]["price"] == pytest.approx(float(df["low"].min()))
        assert out[-1]["i"] == int(df["low"].to_numpy().argmin())

    def test_high_pivot_extends_to_new_higher_high(self):
        df = _df(np.linspace(100, 200, 40))
        pivots = [{"i": 5, "price": 150.0, "dir": -1}]
        out = ta.extend_last_pivot(df, pivots)
        assert out[-1]["price"] == pytest.approx(float(df["high"].max()))


class TestRetracementLeg:
    def test_up_leg_broken_reanchors_to_new_down_leg(self):
        """上涨腿跌破起点后，斐波那契要改画「高点→后续最低」（300308 实测）。"""
        df = _df(np.linspace(200, 80, 60))    # 跌破起点 100
        pivots = [{"i": 10, "price": 100.0, "dir": 1},
                  {"i": 20, "price": 200.0, "dir": -1}]
        leg = ta.retracement_leg(df, pivots)
        assert leg["basis"] == "reanchored"
        assert leg["from"]["price"] == pytest.approx(200.0)
        assert leg["to"]["price"] == pytest.approx(float(df["low"].min()))

    def test_intact_leg_stays_on_pivots(self):
        df = _df(np.linspace(120, 190, 60))     # 没有再破 100，腿还成立
        pivots = [{"i": 10, "price": 100.0, "dir": 1},
                  {"i": 20, "price": 200.0, "dir": -1}]
        leg = ta.retracement_leg(df, pivots)
        assert leg["basis"] == "pivots"
        assert leg["from"] == pivots[0] and leg["to"] == pivots[1]

    def test_single_pivot_returns_none(self):
        assert ta.retracement_leg(_df(np.linspace(100, 120, 30)),
                                  [{"i": 3, "price": 100.0, "dir": 1}]) is None


class TestBands:
    def _df_with_fractals(self):
        # 在 100 附近反复震荡 → 应聚成一条窄带；再在 200 附近震荡 → 另一条
        seg1 = 100 + np.sin(np.arange(60) / 2.0) * 1.0
        seg2 = 200 + np.sin(np.arange(60) / 2.0) * 1.0
        closes = np.concatenate([seg1, np.linspace(105, 195, 20), seg2])
        highs = closes + 0.8
        lows = closes - 0.8
        return _df(closes, highs, lows)

    def test_band_width_is_capped(self):
        """每条带的宽度不能超过 band_max_atr_mult 倍 ATR。"""
        df = self._df_with_fractals()
        atr = ta.atr(df)
        assert atr > 0
        bands = ta.support_resistance_bands(df, atr)
        for b in bands:
            assert b["width_atr"] <= ta.DEFAULTS["band_max_atr_mult"] + 0.05, \
                f"带 {b['lo']}~{b['hi']} 宽 {b['width_atr']} ATR，超过上限"

    def test_bands_have_touch_counts(self):
        df = self._df_with_fractals()
        bands = ta.support_resistance_bands(df, ta.atr(df))
        for b in bands:
            assert b["touches"] >= ta.DEFAULTS["min_touches"]
            assert b["lo"] <= b["hi"]
            assert b["kind"] in ("support", "resistance")


class TestWedge:
    def _falling_wedge_df(self):
        """下降楔形：上轨跌得比下轨快 → 间距一路收窄（两条斜率都是负的，且不会交叉）。"""
        x = np.arange(120, dtype=float)
        upper = 200 - 0.8 * x
        lower = 140 - 0.4 * x
        mid = (upper + lower) / 2
        piv = []
        for i in range(10, 110, 20):
            piv.append({"i": i, "price": float(upper[i]), "dir": 1})
            piv.append({"i": i + 10, "price": float(lower[i + 10]), "dir": -1})
        df = _df(mid)
        df["date"] = [f"D{i}" for i in range(len(df))]
        return df, piv

    def test_falling_wedge_is_converging(self):
        df, piv = self._falling_wedge_df()
        res = ta.trendlines(df, piv, atr_value=5.0)
        assert res["upper"] and res["lower"], "两条轨都该找出来"
        assert res["wedge"] is not None, "两条都下倾但间距收窄，应判为收敛"
        assert res["wedge"]["type"] == "converging"
        assert res["wedge"]["gap_change_pct"] < 0

    def test_parallel_lines_are_not_wedge(self):
        """近平行的两条不该硬判成楔形。"""
        df, piv = self._falling_wedge_df()
        for p in piv:
            base = 200 - 1.0 * p["i"]
            p["price"] = float(base + (30 if p["dir"] > 0 else 0))
        res = ta.trendlines(df, piv, atr_value=5.0)
        assert res["wedge"] is None


class TestChannel:
    """通道几何：第一版拿主段振幅当轨距，轨落在价格外一整段、主图刻度被拉扁；
    第二版只连同侧摆动点，真正的最低点又漏在轨外。现在是「斜率取摆动点、轨位做包络」。"""

    def _rail_at(self, ch, ratio, x):
        p = ch["from"]["price"] + ch["slope"] * (x - ch["from"]["i"])
        off = next(r["offset"] for r in ch["rails"] if abs(r["ratio"] - ratio) < 1e-9)
        return p + off

    def test_channel_encloses_price(self):
        df = _zigzag_df()
        ch = ta.build_annotations(df)["layers"]["fib"]["channel"]
        assert ch is not None
        assert ch["unit"] > 0
        highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
        for x in range(ch["from"]["i"], len(df)):
            assert self._rail_at(ch, 1.0, x) >= highs[x] * (1 - 2e-3), "最高点应被 1.0 轨盖住"
            assert self._rail_at(ch, 0.0, x) <= lows[x] * (1 + 2e-3), "最低点应被 0 轨托住"

    def test_unit_is_not_leg_amplitude(self):
        """轨距是"价格包络高度"，不该等于主段振幅（否则又退回把图拉扁的老路）。"""
        df = _zigzag_df()
        r = ta.build_annotations(df)
        ch = r["layers"]["fib"]["channel"]
        leg = r["major_leg"]
        amp = abs(leg["p1"] - leg["p0"])
        assert ch["unit"] > 0
        assert abs(ch["unit"] - amp) > 1e-6


class TestSwingStructure:
    def test_labels_follow_swing_sequence(self):
        piv = [
            {"i": 10, "price": 100.0, "dir": -1},
            {"i": 20, "price": 120.0, "dir": 1},
            {"i": 30, "price": 110.0, "dir": -1},   # 比上一个低点高 → HL
            {"i": 40, "price": 130.0, "dir": 1},    # 比上一个高点高 → HH
            {"i": 50, "price": 90.0, "dir": -1},    # 跌破上一个低点 → LL
            {"i": 60, "price": 115.0, "dir": 1},    # 没超过上一个高点 → LH
        ]
        got = [s["kind"] for s in ta.swing_structure(piv)]
        assert got == ["L", "H", "HL", "HH", "LL", "LH"]


class TestRegressionChannel:
    def test_uptrend_has_positive_slope_and_band(self):
        x = np.arange(120, dtype=float)
        closes = 100 + 0.5 * x + np.sin(x / 3.0) * 1.2
        df = _df(closes)
        reg = ta.regression_channel(df, window=60)
        assert reg is not None
        assert reg["slope"] > 0
        assert reg["sigma"] > 0
        assert reg["r2"] > 0.8
        assert reg["from"]["i"] == len(df) - 60
        assert reg["to"]["i"] == len(df) - 1


class TestGaps:
    def test_detects_up_gap_and_fill(self):
        # 第 5 根：最低 110 > 前高 101 → 向上跳空；第 6 根跌回 99 → 回补
        closes = [100, 100, 100, 100, 100, 105, 100, 99]
        highs = [101, 101, 101, 101, 101, 112, 112, 100]
        lows = [99, 99, 99, 99, 99, 110, 99, 98]
        g = ta.gaps(_df(closes, highs, lows))
        assert len(g) == 1
        assert g[0]["i"] == 5 and g[0]["kind"] == "up"
        assert g[0]["lo"] == 101 and g[0]["hi"] == 110
        assert g[0]["filled"] is True
        assert g[0]["size_pct"] > 0


class TestBuildAnnotations:
    def test_short_data_degrades(self):
        r = ta.build_annotations(_df([1, 2, 3]))
        assert r["ok"] is False
        assert "数据不足" in r["reason"]

    def test_full_payload_shape(self):
        r = ta.build_annotations(_zigzag_df())
        assert r["ok"] is True
        assert r["atr"] > 0
        layers = r["layers"]
        assert set(layers) == {"fib", "trend", "bands", "structure", "regression", "gaps"}
        assert layers["fib"]["retracement"] is not None
        assert set(layers["fib"]["retracement"]) >= {"levels", "based_on", "up"}
        assert "params" in r

    def test_fib_levels_are_monotonic(self):
        """回撤位按比例单调：上涨段递减、下跌段递增（方向由 up 决定）。"""
        r = ta.build_annotations(_zigzag_df())
        retr = r["layers"]["fib"]["retracement"]
        prices = [lv["price"] for lv in retr["levels"]]
        ordered = sorted(prices, reverse=retr["up"])
        assert prices == ordered, f"up={retr['up']} 时回撤位应单调"

"""A2 校准统计测试：分桶 / insufficient 守卫 / over-under 判定 / hold 排除 / 空输入 / ECE。

状态红线（G3）：n<10 或 Wilson 宽度>0.4 → insufficient；|偏差|>15pp → over/under；其余 healthy。
"""
import math

import pytest

from signal_eval.stats import calc_calibration, wilson_ci


def _item(conf, win, decision="buy", mw=0):
    """构造校准样本项（win=None 表示无收益窗口）。"""
    return {"confidence": conf, "realized_win": win, "decision": decision, "is_market_wide": mw}


# ── 空输入 ──

def test_empty_input():
    r = calc_calibration([])
    assert r["sample_total"] == 0
    assert r["ece"] is None
    assert [b["n"] for b in r["buckets"]] == [0, 0, 0, 0]
    assert all(b["status"] == "insufficient" for b in r["buckets"])
    assert all(b["realized"] is None for b in r["buckets"])


def test_all_no_return_windows():
    """realized_win 全 None（无收益窗口）→ 等价空输入。"""
    r = calc_calibration([_item(0.9, None), _item(0.5, None)])
    assert r["sample_total"] == 0
    assert r["ece"] is None


# ── 分桶边界 ──

def test_bucket_boundaries():
    """0.2/0.5/0.7/1.0 各落一桶；左闭右开（0.4 归第二桶）；range 标签正确。"""
    items = [_item(0.2, True), _item(0.4, True), _item(0.5, True),
             _item(0.7, True), _item(0.9, True), _item(1.0, True)]
    r = calc_calibration(items)
    ranges = [b["range"] for b in r["buckets"]]
    assert ranges == ["0.0-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"]
    ns = [b["n"] for b in r["buckets"]]
    assert ns == [1, 2, 1, 2]
    assert r["sample_total"] == 6


def test_bucket_avg_pred_and_realized():
    items = [_item(0.9, True), _item(0.8, True), _item(0.85, False), _item(1.0, False)]
    r = calc_calibration(items)
    hi = r["buckets"][3]
    assert hi["n"] == 4
    assert hi["avg_pred"] == pytest.approx((0.9 + 0.8 + 0.85 + 1.0) / 4)
    assert hi["realized"] == pytest.approx(0.5)
    lo, hiw = hi["wilson"]
    ref_lo, ref_hi = wilson_ci(2, 4)
    assert math.isclose(lo, ref_lo, abs_tol=1e-3) and math.isclose(hiw, ref_hi, abs_tol=1e-3)


# ── insufficient 守卫（G3 红线）──

def test_insufficient_by_small_n():
    """n=6 < 10 → insufficient（AC-4：不出偏差判定，偏差数字由前端隐藏）。"""
    items = [_item(0.9, i % 2 == 0) for i in range(6)]
    r = calc_calibration(items)
    assert r["buckets"][3]["n"] == 6
    assert r["buckets"][3]["status"] == "insufficient"


def test_insufficient_by_wilson_width():
    """n=12、胜率 50% → Wilson 宽度约 65pp > 40pp → insufficient（G3 红线优先于偏差）。"""
    items = [_item(0.85, i % 2 == 0) for i in range(12)]
    r = calc_calibration(items)
    assert r["buckets"][3]["n"] == 12
    lo, hi = r["buckets"][3]["wilson"]
    assert (hi - lo) > 0.4
    assert r["buckets"][3]["status"] == "insufficient"


def test_over_when_all_lose():
    """全败桶（realized=0）：预测 0.85 → 偏差 +85pp，n=20 Wilson 宽度合格 → over。"""
    items = [_item(0.85, False) for _ in range(20)]
    r = calc_calibration(items)
    assert r["buckets"][3]["status"] == "over"
    assert r["buckets"][3]["deviation_pp"] > 15


# ── over / under / healthy 判定（AC-3）──

def test_over_flagged():
    """n=30、预测 0.85、实现 50% → Wilson 宽度合格且偏差 35pp → over 标红。"""
    items = [_item(0.85, i % 2 == 0) for i in range(30)]
    r = calc_calibration(items)
    b = r["buckets"][3]
    assert b["n"] == 30
    assert (b["wilson"][1] - b["wilson"][0]) <= 0.4
    assert b["deviation_pp"] > 15
    assert b["status"] == "over"


def test_under_flagged():
    """n=20、预测 0.45、实现 90% → 偏差 -45pp → under。"""
    items = [_item(0.45, i % 10 != 0) for i in range(20)]   # 18/20 = 90%
    r = calc_calibration(items)
    b = r["buckets"][1]
    assert b["n"] == 20
    assert b["deviation_pp"] < -15
    assert b["status"] == "under"


def test_healthy():
    """n=20、预测 0.66、实现 60% → 偏差 6pp 且 Wilson 宽度合格 → healthy。"""
    items = [_item(0.66, i % 10 < 6) for i in range(20)]    # 12/20 = 60%
    r = calc_calibration(items)
    b = r["buckets"][2]
    assert b["status"] == "healthy"
    assert abs(b["deviation_pp"]) <= 15


# ── hold / 大盘类排除（AC-5）──

def test_hold_and_market_wide_excluded():
    """hold 与 is_market_wide=1 即使传入也不入桶（双保险，AC-5）。"""
    items = [_item(0.9, True), _item(0.9, True, decision="hold"), _item(0.9, True, mw=1)]
    r = calc_calibration(items)
    assert r["buckets"][3]["n"] == 1
    assert r["sample_total"] == 1


# ── ECE ──

def test_ece_weighted_over_healthy_buckets():
    """ECE 只对非 insufficient 桶按 n 加权：两桶各 20 条、偏差 +8pp/-22pp → 加权平均。"""
    items = [_item(0.68, i % 10 < 6) for i in range(20)]          # 桶 0.6-0.8：pred 68% vs 60% → +8pp healthy
    items += [_item(0.48, i % 10 < 7) for i in range(20)]         # 桶 0.4-0.6：pred 48% vs 70% → -22pp under
    r = calc_calibration(items)
    b_mid = r["buckets"][2]
    b_low = r["buckets"][1]
    assert b_mid["status"] == "healthy"
    assert b_low["status"] == "under"
    expected = (20 * abs(b_mid["deviation_pp"]) + 20 * abs(b_low["deviation_pp"])) / 40 / 100
    assert math.isclose(r["ece"], expected, abs_tol=1e-4)


def test_ece_none_when_all_insufficient():
    items = [_item(0.9, i % 2 == 0) for i in range(6)]
    r = calc_calibration(items)
    assert r["ece"] is None


# ── /calibration 端点（AC-7：缓存命中 <500ms；AC-5：hold 不进分桶） ──

def test_calibration_sync_cached_fast(monkeypatch):
    """结果级 TTL 缓存命中时 build_calibration 耗时 <500ms。"""
    import time

    from server import signal_eval_service as svc

    sigs = [
        {"id": 1, "decision": "buy", "confidence": 0.85, "is_market_wide": 0, "signal_time": "2026-08-01"},
        {"id": 2, "decision": "hold", "confidence": 0.6, "is_market_wide": 0, "signal_time": "2026-08-02"},
        {"id": 3, "decision": "buy", "confidence": None, "is_market_wide": 0, "signal_time": "2026-08-03"},
    ]
    monkeypatch.setattr(svc, "_load_signals", lambda: sigs)
    monkeypatch.setattr(svc, "_signal_returns",
                        lambda s, cost=0.0: {"ret": {"now": 5.0}})
    svc._calibration_cache.clear()

    first = svc.build_calibration(0.0)
    assert first["excluded"]["hold_count"] == 1
    assert first["excluded"]["no_confidence_count"] == 1
    assert first["sample_total"] == 1

    t0 = time.perf_counter()
    svc.build_calibration(0.0)   # 缓存命中
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5   # AC-7

    svc._calibration_cache.clear()

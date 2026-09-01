"""统计计算测试：收益/胜率/Wilson CI/期望值/配对。"""
import pytest

from signal_eval.stats import (
    calc_returns, win_rate, wilson_ci, expectancy_payoff, pair_signals, paired_return,
)

KLINE = [
    {"date": "2026-07-20", "close": 100.0},
    {"date": "2026-07-21", "close": 102.0},
    {"date": "2026-07-22", "close": 105.0},
    {"date": "2026-07-23", "close": 103.0},
    {"date": "2026-07-24", "close": 106.0},
    {"date": "2026-07-27", "close": 110.0},
]


def test_calc_returns_cycles_and_peak():
    r = calc_returns(KLINE, signal_date="2026-07-20", cycles=[1, 2, 5])
    assert r["ret"][1] == pytest.approx(2.0)      # 102/100-1
    assert r["ret"][2] == pytest.approx(5.0)      # 105/100-1
    assert r["ret"][5] == pytest.approx(10.0)     # 110/100-1
    assert r["ret"]["now"] == pytest.approx(10.0)
    assert r["peak"] == pytest.approx(10.0)
    assert r["maxDD"] == pytest.approx(0.0)       # 07-23 回调 105→103 但未低于 100


def test_calc_returns_missing_kline_date():
    r = calc_returns(KLINE, signal_date="2026-09-01")
    assert r["ret"] == {}
    assert r["peak"] is None


def test_calc_returns_insufficient_cycles():
    """K 线不足 N 日 → 该周期置 None（不入 ret）。"""
    r = calc_returns(KLINE, signal_date="2026-07-20", cycles=[1, 2, 99])
    assert 99 not in r["ret"]
    assert 1 in r["ret"]


def test_win_rate():
    assert win_rate([1.0, 2.0, -0.5, 3.0]) == pytest.approx(0.75)
    assert win_rate([]) is None
    assert win_rate([-1.0, -2.0]) == 0.0


def test_wilson_ci_bounds():
    lo, hi = wilson_ci(4, 6)
    assert 0 <= lo < hi <= 1
    assert lo < 4 / 6 < hi
    # 边界：n=1
    lo2, hi2 = wilson_ci(1, 1)
    assert 0 <= lo2 < 1.0


def test_expectancy_payoff():
    exp, payoff = expectancy_payoff([10.0, 5.0, -5.0, -15.0])
    assert exp == pytest.approx(-1.25)            # mean
    assert payoff == pytest.approx(0.75)          # 7.5 / 10
    assert expectancy_payoff([]) == (None, None)


def test_pair_signals_buy_sell():
    sigs = [
        {"id": 1, "symbol_key": "sz300442", "decision": "buy", "signal_time": "2026-07-20T10:00:00"},
        {"id": 2, "symbol_key": "sz300442", "decision": "sell", "signal_time": "2026-07-28T10:00:00"},
        {"id": 3, "symbol_key": "sz300442", "decision": "buy", "signal_time": "2026-08-02T10:00:00"},
    ]
    pairs = pair_signals(sigs)
    assert len(pairs) == 1
    assert pairs[0][0]["id"] == 1 and pairs[0][1]["id"] == 2


def test_pair_signals_buy_sell_buy_sell():
    """buy→sell→buy→sell：两对独立配对。"""
    sigs = [
        {"id": 1, "symbol_key": "x", "decision": "buy", "signal_time": "2026-07-01"},
        {"id": 2, "symbol_key": "x", "decision": "sell", "signal_time": "2026-07-10"},
        {"id": 3, "symbol_key": "x", "decision": "buy", "signal_time": "2026-07-15"},
        {"id": 4, "symbol_key": "x", "decision": "sell", "signal_time": "2026-07-25"},
    ]
    pairs = pair_signals(sigs)
    assert len(pairs) == 2


def test_pair_signals_unclosed_buy_no_pair():
    sigs = [
        {"id": 1, "symbol_key": "x", "decision": "buy", "signal_time": "2026-07-01"},
    ]
    assert pair_signals(sigs) == []


def test_paired_return_uses_sell_minus_buy():
    """通道① 配对收益 = 卖出日收盘 − 买入日收盘（已实现），非浮动。"""
    r = paired_return(KLINE, buy_date="2026-07-20", sell_date="2026-07-24")
    assert r == pytest.approx(6.0)  # (106-100)/100


def test_paired_return_missing_date():
    assert paired_return(KLINE, buy_date="2026-09-01", sell_date="2026-09-02") is None

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


# ── 交易成本（P2：净收益 = 毛收益 − 2×单边费率，买卖各一次） ──

def test_calc_returns_with_cost():
    """毛收益 10%、单边 0.1% → 净 9.8%。"""
    r = calc_returns(KLINE, signal_date="2026-07-20", cycles=[5], cost_pct=0.1)
    assert r["ret"][5] == pytest.approx(9.8)
    assert r["ret"]["now"] == pytest.approx(9.8)


def test_calc_returns_cost_zero_unchanged():
    """cost=0（默认）与原口径完全一致。"""
    a = calc_returns(KLINE, signal_date="2026-07-20", cycles=[5])
    b = calc_returns(KLINE, signal_date="2026-07-20", cycles=[5], cost_pct=0.0)
    assert a == b


def test_paired_return_with_cost():
    """配对毛收益 6%、单边 0.1% → 净 5.8%。"""
    r = paired_return(KLINE, buy_date="2026-07-20", sell_date="2026-07-24", cost_pct=0.1)
    assert r == pytest.approx(5.8)


def test_net_ret_helper():
    """毛→净换算：route 层缓存毛收益，展示时按 cost 扣减。"""
    from signal_eval.stats import net_ret
    assert net_ret(10.0, 0.1) == pytest.approx(9.8)
    assert net_ret(10.0, 0.0) == pytest.approx(10.0)


# ── 组合净值（P5：等权独立子仓位，buy 收盘建仓 / sell 收盘平仓，hold 不持仓） ──

def _sig(sym, id_, decision, date):
    return {"id": id_, "symbol_key": sym, "decision": decision, "signal_time": date}


def test_portfolio_equity_single_paired():
    """buy→sell 配对：持有期按日浮动、卖出日落袋、之后保持已实现值。"""
    from signal_eval.stats import portfolio_equity
    sigs = [_sig("sh600519", 1, "buy", "2026-07-20"),
            _sig("sh600519", 2, "sell", "2026-07-24")]
    r = portfolio_equity(sigs, {"sh600519": KLINE})
    assert r["dates"] == ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-27"]
    assert r["equity"][0] == pytest.approx(1.0)
    assert r["equity"][1] == pytest.approx(1.02)
    assert r["equity"][3] == pytest.approx(1.03)
    assert r["equity"][4] == pytest.approx(1.06)   # 卖出日落袋 6%
    assert r["equity"][5] == pytest.approx(1.06)   # 平仓后保持（不再吃 07-27 的 110）
    assert r["return"] == pytest.approx(6.0)
    assert r["max_dd"] == pytest.approx(1.9)   # 峰 1.05 → 谷 1.03（保留 2 位小数）
    assert r["open_positions"] == 0


def test_portfolio_equity_with_cost():
    """费后：已实现 6% − 2×0.1 = 5.8%；持有期浮动仍为毛值。"""
    from signal_eval.stats import portfolio_equity
    sigs = [_sig("sh600519", 1, "buy", "2026-07-20"),
            _sig("sh600519", 2, "sell", "2026-07-24")]
    r = portfolio_equity(sigs, {"sh600519": KLINE}, cost_pct=0.1)
    assert r["equity"][3] == pytest.approx(1.03)    # 持有期毛浮动
    assert r["equity"][4] == pytest.approx(1.058)   # 落袋扣费
    assert r["return"] == pytest.approx(5.8)


def test_portfolio_equity_open_position():
    """未平仓 buy：全程按日浮动，期末 open_positions=1。"""
    from signal_eval.stats import portfolio_equity
    sigs = [_sig("sh600519", 1, "buy", "2026-07-20")]
    r = portfolio_equity(sigs, {"sh600519": KLINE})
    assert r["equity"][-1] == pytest.approx(1.10)
    assert r["return"] == pytest.approx(10.0)
    assert r["open_positions"] == 1


def test_portfolio_equity_max_dd():
    """最大回撤：峰 1.10 → 谷 0.95 → 13.64%。"""
    from signal_eval.stats import portfolio_equity
    kline = [
        {"date": "2026-08-03", "close": 100.0},
        {"date": "2026-08-04", "close": 110.0},
        {"date": "2026-08-05", "close": 95.0},
        {"date": "2026-08-06", "close": 105.0},
    ]
    r = portfolio_equity([_sig("x", 1, "buy", "2026-08-03")], {"x": kline})
    assert r["max_dd"] == pytest.approx(13.64)   # 峰 1.10 → 谷 0.95（保留 2 位小数）


def test_portfolio_equity_overlapping_buys():
    """同标的重叠 buy：各自独立子仓位（buy1→sell 配对，buy2 未平仓）。"""
    from signal_eval.stats import portfolio_equity
    kline = [
        {"date": "2026-08-03", "close": 100.0},
        {"date": "2026-08-04", "close": 110.0},
        {"date": "2026-08-05", "close": 120.0},
        {"date": "2026-08-06", "close": 132.0},
    ]
    sigs = [_sig("x", 1, "buy", "2026-08-03"),
            _sig("x", 2, "buy", "2026-08-04"),
            _sig("x", 3, "sell", "2026-08-06")]
    r = portfolio_equity(sigs, {"x": kline})
    assert r["equity"][1] == pytest.approx(1.10)
    # 08-05: pos1 +20% + pos2 120/110−1
    assert r["equity"][2] == pytest.approx(1.20 + (120 / 110 - 1), rel=1e-4)
    # 08-06: pos1 落袋 32% + pos2 132/110−1
    assert r["equity"][3] == pytest.approx(1.32 + (132 / 110 - 1), rel=1e-4)
    assert r["open_positions"] == 1


def test_portfolio_equity_multi_symbol_union_dates():
    """多标的：日期轴取并集；某标的缺某日 → 前向填充最近收盘。"""
    from signal_eval.stats import portfolio_equity
    k1 = [{"date": "2026-08-03", "close": 100.0},
          {"date": "2026-08-04", "close": 101.0}]
    k2 = [{"date": "2026-08-03", "close": 50.0},
          {"date": "2026-08-04", "close": 51.0},
          {"date": "2026-08-05", "close": 52.0}]
    sigs = [_sig("a", 1, "buy", "2026-08-03"), _sig("b", 2, "buy", "2026-08-03")]
    r = portfolio_equity(sigs, {"a": k1, "b": k2})
    assert r["dates"] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    # 08-05: a 前向填充 101 → +1%；b +4%
    assert r["equity"][2] == pytest.approx(1.05)


def test_portfolio_equity_ignores_hold_and_empty():
    """hold 不建仓；无 K 线/无信号返回空序列。"""
    from signal_eval.stats import portfolio_equity
    r = portfolio_equity([_sig("x", 1, "hold", "2026-08-03")],
                         {"x": [{"date": "2026-08-03", "close": 100.0}]})
    assert r["dates"] == [] and r["equity"] == [] and r["return"] is None
    r2 = portfolio_equity([_sig("x", 1, "buy", "2026-08-03")], {})
    assert r2["equity"] == []
    r3 = portfolio_equity([], {})
    assert r3["equity"] == []


# ── fetch_kline 市场前缀剥离（2026-09-06 前缀 bug 修复回归）──

def test_bare_code_strips_market_prefix():
    from signal_eval.stats import _bare_code
    assert _bare_code("sh600519") == "600519"
    assert _bare_code("SZ002714") == "002714"
    assert _bare_code("bj920069") == "920069"
    assert _bare_code("600519") == "600519"   # 裸码原样
    assert _bare_code("") == ""
    assert _bare_code(None) == ""


def test_fetch_kline_passes_bare_code(monkeypatch):
    """fetch_kline 必须把带前缀 symbol_key 剥成裸码再调 _get_kline_sync。"""
    calls = {}

    def fake_get_kline_sync(code, days, start_date, end_date, adjust, market):
        calls["code"] = code
        return {"kline": [{"date": "2026-09-01", "close": 1.0}]}

    monkeypatch.setattr("server.routes.charts._get_kline_sync", fake_get_kline_sync)
    from signal_eval.stats import fetch_kline
    kline = fetch_kline("sh600519")
    assert calls["code"] == "600519"
    assert len(kline) == 1


def test_fetch_kline_empty_result(monkeypatch):
    monkeypatch.setattr("server.routes.charts._get_kline_sync",
                        lambda *a, **kw: None)
    from signal_eval.stats import fetch_kline
    assert fetch_kline("sz002714") == []

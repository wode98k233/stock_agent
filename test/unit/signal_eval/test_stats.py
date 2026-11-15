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


# ── 数据状态（P0 收尾：数据覆盖/新鲜度门控的原料） ──

def test_calc_returns_status_missing_without_kline():
    """无 K 线 → status=missing，没有 last_bar_date/bars。"""
    r = calc_returns([], signal_date="2026-07-20")
    assert r["status"] == "missing"
    assert r["last_bar_date"] is None
    assert r["bars_after_entry"] == 0
    assert r["entry_price"] is None
    assert r["ret"] == {}


def test_calc_returns_status_missing_without_entry_point():
    """信号晚于全部 K 线（无入场点）→ missing，但保留最后一根 K 线日期。"""
    r = calc_returns(KLINE, signal_date="2026-09-01")
    assert r["status"] == "missing"
    assert r["last_bar_date"] == "2026-07-27"
    assert r["bars_after_entry"] == 0


def test_calc_returns_bars_after_entry_and_last_bar():
    """正常入场：bars_after_entry = 入场 bar 到最后一根 K 线的 bar 数。"""
    r = calc_returns(KLINE, signal_date="2026-07-20")
    assert r["status"] == "ok"
    assert r["last_bar_date"] == "2026-07-27"
    assert r["bars_after_entry"] == 5      # 共 6 根，入场在第 0 根
    assert r["entry_mode"] == "legacy_close"


def test_calc_returns_entry_on_last_bar_has_no_5d():
    """入场就在最后一根：ok / bars=0，5 日收益缺失（服务层会判 pending）。"""
    r = calc_returns(KLINE, signal_date="2026-07-27")
    assert r["status"] == "ok"
    assert r["bars_after_entry"] == 0
    assert r["last_bar_date"] == "2026-07-27"
    assert 5 not in r["ret"]
    assert r["ret"]["now"] == pytest.approx(0.0)


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


def test_pair_signals_cross_symbol_not_paired():
    """buy(A) → sell(B)：跨标的绝不能配对（配出来的收益会变成两个标的的价差）。"""
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy", "signal_time": "2026-07-01"},
        {"id": 2, "symbol_key": "sz002714", "decision": "sell", "signal_time": "2026-07-10"},
    ]
    assert pair_signals(sigs) == []


def test_pair_signals_same_symbol_fifo():
    """buy(A) → buy(A) → sell(A)：FIFO 配最早那笔，第二笔仍 open。"""
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy", "signal_time": "2026-07-01"},
        {"id": 2, "symbol_key": "sh600519", "decision": "buy", "signal_time": "2026-07-05"},
        {"id": 3, "symbol_key": "sh600519", "decision": "sell", "signal_time": "2026-07-10"},
    ]
    pairs = pair_signals(sigs)
    assert len(pairs) == 1
    assert (pairs[0][0]["id"], pairs[0][1]["id"]) == (1, 3)


def test_pair_signals_sell_before_buy_not_paired():
    """sell(A) → buy(A)：卖出在前，没有可平仓的买入 → 不配对。"""
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "sell", "signal_time": "2026-07-01"},
        {"id": 2, "symbol_key": "sh600519", "decision": "buy", "signal_time": "2026-07-10"},
    ]
    assert pair_signals(sigs) == []


def test_pair_signals_same_time_tiebreak_by_id():
    """同一 signal_time 用 id 兜底排序，FIFO 结果可复现。"""
    sigs = [
        {"id": 7, "symbol_key": "A", "decision": "buy", "signal_time": "2026-07-01"},
        {"id": 5, "symbol_key": "A", "decision": "buy", "signal_time": "2026-07-01"},
        {"id": 9, "symbol_key": "A", "decision": "sell", "signal_time": "2026-07-10"},
    ]
    pairs = pair_signals(sigs)
    assert len(pairs) == 1
    assert (pairs[0][0]["id"], pairs[0][1]["id"]) == (5, 9)   # id 小的先入队


def test_pair_signals_multi_symbol_independent():
    """A/B 两组混合：各自独立 FIFO 配对，跨标的绝不互配；输出按 buy 时间排序。"""
    sigs = [
        {"id": 2, "symbol_key": "B", "decision": "buy", "signal_time": "2026-07-02"},
        {"id": 1, "symbol_key": "A", "decision": "buy", "signal_time": "2026-07-01"},
        {"id": 3, "symbol_key": "B", "decision": "sell", "signal_time": "2026-07-03"},
        {"id": 4, "symbol_key": "A", "decision": "sell", "signal_time": "2026-07-04"},
        {"id": 5, "symbol_key": "A", "decision": "buy", "signal_time": "2026-07-05"},
    ]
    pairs = pair_signals(sigs)
    assert [(b["id"], s["id"]) for b, s in pairs] == [(1, 4), (2, 3)]   # 按 buy 时间排序
    assert all(b["symbol_key"] == s["symbol_key"] for b, s in pairs)
    assert 5 not in {b["id"] for b, _ in pairs}         # A 的第二笔 buy 仍 open


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
    """buy→sell 配对（N=1）：持有期按日浮动、卖出日落袋、之后保持已实现值。"""
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
    assert r["position_count"] == 1            # 等权口径的 N


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
    """同标的重叠 buy：各自独立子仓位（buy1→sell 配对，buy2 未平仓）；N=2 等权。"""
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
    assert r["equity"][0] == pytest.approx(1.0)    # 08-03: pos1 当日建仓，浮动 0
    # 08-04: pos1 +10% + pos2 建仓当日 0，等权平均 → +5%
    assert r["equity"][1] == pytest.approx(1.05)
    # 08-05: pos1 +20% + pos2 120/110−1；各 1/2
    assert r["equity"][2] == pytest.approx(1 + ((0.20 + (120 / 110 - 1)) / 2), rel=1e-4)
    # 08-06: pos1 落袋 32% + pos2 132/110−1
    assert r["equity"][3] == pytest.approx(1 + ((0.32 + (132 / 110 - 1)) / 2), rel=1e-4)
    assert r["open_positions"] == 1
    assert r["position_count"] == 2


def test_portfolio_equity_multi_symbol_union_dates():
    """多标的：日期轴取并集；某标的缺某日 → 前向填充最近收盘；N=2 等权。"""
    from signal_eval.stats import portfolio_equity
    k1 = [{"date": "2026-08-03", "close": 100.0},
          {"date": "2026-08-04", "close": 101.0}]
    k2 = [{"date": "2026-08-03", "close": 50.0},
          {"date": "2026-08-04", "close": 51.0},
          {"date": "2026-08-05", "close": 52.0}]
    sigs = [_sig("a", 1, "buy", "2026-08-03"), _sig("b", 2, "buy", "2026-08-03")]
    r = portfolio_equity(sigs, {"a": k1, "b": k2})
    assert r["dates"] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    # 08-05: a 前向填充 101 → +1%；b +4%；等权 (1%+4%)/2 = 2.5%
    assert r["equity"][2] == pytest.approx(1.025)
    assert r["position_count"] == 2


def test_portfolio_equity_equal_weight_offsets():
    """等权：2 个仓位一涨 10% 一跌 10% → 组合净值约 1.0（不是简单相加）。"""
    from signal_eval.stats import portfolio_equity
    k_up = [{"date": "2026-08-03", "close": 100.0},
            {"date": "2026-08-04", "close": 110.0}]
    k_down = [{"date": "2026-08-03", "close": 100.0},
              {"date": "2026-08-04", "close": 90.0}]
    sigs = [_sig("up", 1, "buy", "2026-08-03"), _sig("down", 2, "buy", "2026-08-03")]
    r = portfolio_equity(sigs, {"up": k_up, "down": k_down})
    assert r["equity"] == pytest.approx([1.0, 1.0])
    assert r["return"] == pytest.approx(0.0)
    assert r["max_dd"] <= 100
    assert r["position_count"] == 2


def test_portfolio_equity_equal_weight_half_position():
    """等权：2 个仓位只有 1 个涨 10%（另一个空仓的现金不动）→ 组合约 +5%。"""
    from signal_eval.stats import portfolio_equity
    k_up = [{"date": "2026-08-03", "close": 100.0},
            {"date": "2026-08-04", "close": 110.0}]
    k_flat = [{"date": "2026-08-03", "close": 100.0},
              {"date": "2026-08-04", "close": 100.0}]
    sigs = [_sig("up", 1, "buy", "2026-08-03"), _sig("flat", 2, "buy", "2026-08-03")]
    r = portfolio_equity(sigs, {"up": k_up, "flat": k_flat})
    assert r["equity"][-1] == pytest.approx(1.05)
    assert r["return"] == pytest.approx(5.0)      # 半仓收益，不是 10%
    assert r["max_dd"] <= 100                     # 等权口径下回撤不可能 >100%


def test_portfolio_equity_ignores_hold_and_empty():
    """hold 不建仓；无 K 线/无信号返回空序列（position_count=0）。"""
    from signal_eval.stats import portfolio_equity
    r = portfolio_equity([_sig("x", 1, "hold", "2026-08-03")],
                         {"x": [{"date": "2026-08-03", "close": 100.0}]})
    assert r["dates"] == [] and r["equity"] == [] and r["return"] is None
    assert r["position_count"] == 0
    r2 = portfolio_equity([_sig("x", 1, "buy", "2026-08-03")], {})
    assert r2["equity"] == []
    assert r2["position_count"] == 0
    r3 = portfolio_equity([], {})
    assert r3["equity"] == []
    assert r3["position_count"] == 0


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


# ── 可执行时点入场/出场（报告多在收盘后生成，不能再用信号日收盘成交）──
#
# 背景：信号是「报告生成时」打的时间戳，实测主样本 65% 落在北京时间 15:00 之后
# 或凌晨。此时当日收盘已经发生，用信号日收盘价成交＝买在已知收盘上（前视且不可
# 执行）。所以入场/出场价按「可执行时点」判定：
#   盘中(09:30–15:00) 当日收盘 / 开盘前(<09:30) 当日开盘 / 收盘后(≥15:00)或非交易日 → 次日开盘
#   只有日期（旧格式）→ 当日收盘（legacy_close，兼容历史数据）

KLINE_OC = [
    {"date": "2026-07-17", "open": 98.0, "close": 99.0},     # 周五
    {"date": "2026-07-20", "open": 100.0, "close": 104.0},   # 周一（跨周末后的第一个交易日）
    {"date": "2026-07-21", "open": 106.0, "close": 108.0},
    {"date": "2026-07-22", "open": 103.0, "close": 101.0},
    {"date": "2026-07-23", "open": 100.0, "close": 102.0},
    {"date": "2026-07-24", "open": 104.0, "close": 107.0},   # 周五
    {"date": "2026-07-27", "open": 110.0, "close": 112.0},   # 下周一
]


def test_action_point_intraday_uses_same_day_close():
    """盘中 BJ 10:00（UTC 02:00）→ 当日收盘可成交。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-20T02:00:00+00:00") == (
        1, "2026-07-20", 104.0, "close")


def test_action_point_after_close_uses_next_open():
    """收盘后 BJ 21:00（UTC 13:00）→ 当日已收盘，只能下一交易日开盘成交。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-20T13:00:00+00:00") == (
        2, "2026-07-21", 106.0, "next_open")


def test_action_point_pre_open_uses_same_day_open():
    """开盘前 BJ 08:00（UTC 00:00）→ 当日开盘可成交。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-20T00:00:00+00:00") == (
        1, "2026-07-20", 100.0, "open")


def test_action_point_date_only_keeps_legacy_close():
    """只有日期（旧格式）→ 保持旧口径（当日收盘），历史数据口径不变。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-20") == (
        1, "2026-07-20", 104.0, "legacy_close")


def test_action_point_friday_after_close_skips_weekend_to_monday_open():
    """周五收盘后 → 跳过周末，下一个交易日（周一）开盘。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-24T13:00:00+00:00") == (
        6, "2026-07-27", 110.0, "next_open")


def test_action_point_weekend_uses_next_trading_day_open():
    """非交易日（周六）盘中时刻 → 当日无 K 线 → 下周一开盘。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-25T02:00:00+00:00") == (
        6, "2026-07-27", 110.0, "next_open")


def test_action_point_naive_datetime_assumed_beijing():
    """无时区（含空格分隔）→ 视为北京时间；带时区先转 Asia/Shanghai。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-20T10:00:00") == (
        1, "2026-07-20", 104.0, "close")
    assert _action_point(KLINE_OC, "2026-07-20 10:00:00") == (
        1, "2026-07-20", 104.0, "close")
    # 同一时刻换成 UTC 表示（02:00Z）仍然落在盘中 10:00
    assert _action_point(KLINE_OC, "2026-07-20T02:00:00Z") == (
        1, "2026-07-20", 104.0, "close")


def test_action_point_1500_boundary_is_after_close():
    """15:00 整属于「收盘后」：收盘集合竞价已结束，当日无可成交价。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE_OC, "2026-07-20T15:00:00+08:00")[3] == "next_open"


def test_action_point_missing_open_falls_back_to_close():
    """旧 K 线没有 open 字段 → 用 close 兜底（mode 仍标 open/next_open）。"""
    from signal_eval.stats import _action_point
    assert _action_point(KLINE, "2026-07-20T00:00:00+00:00") == (
        0, "2026-07-20", 100.0, "open")          # 开盘前，无 open → close
    assert _action_point(KLINE, "2026-07-20T13:00:00+00:00") == (
        1, "2026-07-21", 102.0, "next_open")     # 收盘后，无 open → close


def test_calc_returns_intraday_entry_fields():
    """盘中信号：入场 = 当日收盘，周期从入场 bar 起算。"""
    r = calc_returns(KLINE_OC, "2026-07-20T02:00:00+00:00", cycles=[1])
    assert (r["entry_date"], r["entry_price"], r["entry_mode"]) == (
        "2026-07-20", 104.0, "close")
    assert r["ret"][1] == pytest.approx(3.85)    # 108/104-1
    assert r["ret"]["now"] == pytest.approx(7.69)   # 112/104-1


def test_calc_returns_after_close_shifts_to_next_open():
    """收盘后信号：入场 = 次日开盘；peak/maxDD 也从入场价起算。"""
    r = calc_returns(KLINE_OC, "2026-07-20T13:00:00+00:00", cycles=[1])
    assert (r["entry_date"], r["entry_price"], r["entry_mode"]) == (
        "2026-07-21", 106.0, "next_open")
    assert r["ret"][1] == pytest.approx(-4.72)      # 101/106-1
    assert r["ret"]["now"] == pytest.approx(5.66)   # 112/106-1
    assert r["peak"] == pytest.approx(5.66)         # 入场后最高 112
    assert r["maxDD"] == pytest.approx(-4.72)       # 入场后最低 101


def test_calc_returns_pre_open_uses_same_day_open():
    """开盘前信号：入场 = 当日开盘（不是当日收盘 104）。"""
    r = calc_returns(KLINE_OC, "2026-07-20T00:00:00+00:00", cycles=[1])
    assert (r["entry_date"], r["entry_price"], r["entry_mode"]) == (
        "2026-07-20", 100.0, "open")
    assert r["ret"][1] == pytest.approx(8.0)    # 108/100-1


def test_calc_returns_date_only_is_legacy_close():
    """只有日期 → 旧口径（当日收盘 104），与升级前一致。"""
    r = calc_returns(KLINE_OC, "2026-07-20", cycles=[1])
    assert (r["entry_price"], r["entry_mode"]) == (104.0, "legacy_close")
    assert r["ret"][1] == pytest.approx(3.85)


def test_calc_returns_after_close_no_next_bar():
    """最后一根 K 线收盘后 → 没有可成交的下一根 → ret 为空（不是 0）。"""
    r = calc_returns(KLINE_OC, "2026-07-27T13:00:00+00:00")
    assert r["ret"] == {}
    assert r["peak"] is None and r["maxDD"] is None
    assert r["entry_price"] is None and r["entry_date"] is None
    assert r["entry_mode"] is None


def test_paired_return_uses_executable_points():
    """配对两端都按可执行时点：买入收盘后（次日开盘 106）→ 卖出盘中（当日收盘 101）。"""
    r = paired_return(KLINE_OC, buy_date="2026-07-20T13:00:00+00:00",
                      sell_date="2026-07-22T02:00:00+00:00")
    assert r == pytest.approx(-4.72)    # 101/106-1


def test_paired_return_date_only_still_close():
    """日期-only 入参仍是当日收盘（旧口径兼容）。"""
    r = paired_return(KLINE_OC, buy_date="2026-07-20", sell_date="2026-07-22")
    assert r == pytest.approx(-2.88)    # 101/104-1


def test_portfolio_equity_uses_executable_points():
    """组合净值：买入收盘后 → 次日起才计仓，按次日开盘计成本；卖出盘中按当日收盘。"""
    from signal_eval.stats import portfolio_equity
    sigs = [_sig("x", 1, "buy", "2026-07-20T13:00:00+00:00"),    # 收盘后 → 07-21 开盘 106
            _sig("x", 2, "sell", "2026-07-22T02:00:00+00:00")]   # 盘中 → 07-22 收盘 101
    r = portfolio_equity(sigs, {"x": KLINE_OC})
    assert r["dates"][0] == "2026-07-21"                 # 入场日不是信号日 07-20
    assert r["equity"][0] == pytest.approx(108 / 106)    # 07-21 收盘浮动
    assert r["equity"][1] == pytest.approx(101 / 106)    # 07-22 收盘平仓
    assert r["equity"][-1] == pytest.approx(101 / 106)   # 平仓后保持
    assert r["return"] == pytest.approx(round((101 / 106 - 1) * 100, 2))   # -4.72
    assert r["open_positions"] == 0


def test_portfolio_equity_pre_open_entry_uses_open():
    """组合净值：开盘前买入按当日开盘 100（对比收盘口径 104）。"""
    from signal_eval.stats import portfolio_equity
    sigs = [_sig("x", 1, "buy", "2026-07-20T00:00:00+00:00")]
    r = portfolio_equity(sigs, {"x": KLINE_OC})
    assert r["dates"][0] == "2026-07-20"
    assert r["equity"][0] == pytest.approx(104 / 100)    # 当日收盘浮动
    assert r["return"] == pytest.approx(12.0)            # 112/100-1


def test_portfolio_equity_date_only_still_close():
    """组合净值：日期-only 买入仍是当日收盘建仓（旧口径兼容）。"""
    from signal_eval.stats import portfolio_equity
    r = portfolio_equity([_sig("x", 1, "buy", "2026-07-20")], {"x": KLINE_OC})
    assert r["dates"][0] == "2026-07-20"
    assert r["equity"][0] == pytest.approx(1.0)          # 收盘建仓当天浮动 0
    assert r["return"] == pytest.approx(round((112 / 104 - 1) * 100, 2))   # 7.69

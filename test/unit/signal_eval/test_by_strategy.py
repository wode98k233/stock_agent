"""任务 7.3：按策略聚合的前瞻统计单测（server/signal_eval_service.py::build_by_strategy）。

全部打桩：_load_signals 供信号、_kline_cached 供假 K 线 —— 不碰真库、不碰网络。
口径要点：信号日之后必须已有 holding_days 根 K 线才算「成熟」（进胜率分母），否则进 pending。
"""
from datetime import datetime, timedelta

import pytest

from server import signal_eval_service as svc
from signal_eval.stats import calc_returns, expectancy_payoff, wilson_ci

STRATEGY = "sr_test"


def _kline(start: str, closes: list[float]) -> list[dict]:
    """连续自然日假 K 线（_signal_idx 只按 date[:10] 字符串比较，够用）。"""
    d0 = datetime.strptime(start, "%Y-%m-%d")
    return [{"date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"), "close": c}
            for i, c in enumerate(closes)]


def _sig(symbol_key: str, signal_time: str, strategy_id: str = STRATEGY) -> dict:
    return {"strategy_id": strategy_id, "symbol_key": symbol_key,
            "signal_time": signal_time, "decision": "buy"}


@pytest.fixture
def stub(monkeypatch):
    """打桩容器：signals 列表 + klines（键为裸代码，_symbol_code 会剥 sh/sz 前缀）。"""
    state = {"signals": [], "klines": {}}
    monkeypatch.setattr(svc, "_load_signals", lambda: list(state["signals"]))
    monkeypatch.setattr(svc, "_kline_cached",
                        lambda code, market=None: state["klines"].get(code, []))
    return state


def _mature_rets(signals: list[dict], klines: dict, holding_days: int = 5,
                 cost_pct: float = 0.0) -> list[float]:
    """用同一套纯函数复算成熟样本收益（校验接线，不重算公式）。"""
    out = []
    for s in signals:
        if (s.get("strategy_id") or "") != STRATEGY:
            continue
        rets = calc_returns(klines[s["symbol_key"][2:]], s["signal_time"],
                            cycles=[holding_days], cost_pct=cost_pct)
        ret = (rets.get("ret") or {}).get(holding_days)
        if ret is not None:
            out.append(float(ret))
    return out


def _five_samples(stub) -> None:
    """5 个成熟样本、3 胜 2 负（+10 / -10 / +10 / -10 / +20）。"""
    closes = [10, 10, 10, 10, 10, 11, 9, 11, 9, 12]
    stub["klines"] = {"600519": _kline("2026-09-01", closes)}
    stub["signals"] = [_sig("sh600519", f"2026-09-0{i} 10:00:00") for i in range(1, 6)]


def test_only_matching_strategy_id_counts(stub):
    """① 混入 llm 来源（strategy_id 为空）与别的策略的信号都要被排除。"""
    stub["signals"] = [
        _sig("sh600519", "2026-09-01 10:00:00"),
        _sig("sh600519", "2026-09-01 10:00:00", strategy_id=""),          # llm 对话信号
        _sig("sh600519", "2026-09-01 10:00:00", strategy_id="sr_other"),   # 别的策略
    ]
    stub["klines"] = {"600519": _kline("2026-09-01", [10] * 5 + [11])}  # 第 5 根 +10%
    res = svc.build_by_strategy(STRATEGY, holding_days=5)
    assert res["signal_count"] == 1
    assert res["mature_count"] == 1
    assert res["signals"][0]["symbol_key"] == "sh600519"
    assert res["win_rate"] == 1.0


def test_insufficient_kline_goes_pending(stub):
    """② K 线不足 holding_days 根 → pending，不计入胜率分母。"""
    stub["signals"] = [_sig("sh600519", "2026-09-01 10:00:00")]
    stub["klines"] = {"600519": _kline("2026-09-01", [10, 10, 10])}  # 只有 3 根 < 5
    res = svc.build_by_strategy(STRATEGY, holding_days=5)
    assert res["signal_count"] == 1
    assert res["pending_count"] == 1
    assert res["mature_count"] == 0
    assert res["win_rate"] is None
    assert res["wilson_ci"] == [0.0, 1.0]
    assert res["avg_return"] is None
    assert res["signals"][0]["mature"] is False
    assert res["signals"][0]["ret"] is None


def test_win_rate_and_wilson_ci_match_stats(stub):
    """③ 5 成熟样本 3 胜 → win_rate 0.6；CI 由 stats.wilson_ci 提供（服务层只 round 4 位）。"""
    _five_samples(stub)
    res = svc.build_by_strategy(STRATEGY, holding_days=5)
    assert res["mature_count"] == 5
    assert res["pending_count"] == 0
    assert res["win_rate"] == 0.6
    lo, hi = wilson_ci(3, 5)
    assert res["wilson_ci"] == [round(lo, 4), round(hi, 4)]


def test_avg_return_and_expectancy_match_stats(stub):
    """④ avg_return = 成熟样本均值（2 位）；expectancy/payoff 与 expectancy_payoff 一致。"""
    _five_samples(stub)
    res = svc.build_by_strategy(STRATEGY, holding_days=5)
    rets = _mature_rets(stub["signals"], stub["klines"], holding_days=5)
    assert rets == [10.0, -10.0, 10.0, -10.0, 20.0]
    assert res["avg_return"] == round(sum(rets) / len(rets), 2) == 4.0
    exp, payoff = expectancy_payoff(rets)
    assert res["expectancy"] == round(exp, 4)
    assert res["payoff"] == round(payoff, 4)
    assert res["payoff"] == 1.3333  # 均盈 13.33 / |均亏 10|


def test_cost_pct_reduces_avg_return(stub):
    """⑤ cost_pct > 0 时成本真的扣进去了（每笔扣 2×单边费率）。"""
    _five_samples(stub)
    free = svc.build_by_strategy(STRATEGY, holding_days=5, cost_pct=0.0)
    cost = svc.build_by_strategy(STRATEGY, holding_days=5, cost_pct=0.1)
    assert free["avg_return"] == 4.0
    assert cost["avg_return"] == 3.8
    assert cost["avg_return"] < free["avg_return"]
    assert cost["win_rate"] == free["win_rate"] == 0.6  # 费率不改胜负方向


def test_days_tracked_uses_injected_today(stub):
    """⑥ days_tracked 由最早信号日与注入的 today 算出（不用 date.today()）。"""
    stub["klines"] = {"600519": _kline("2026-09-01", [10] * 7)}
    stub["signals"] = [_sig("sh600519", "2026-09-01 10:00:00"),
                       _sig("sh600519", "2026-09-10 10:00:00")]  # 无对应 K 线 → pending
    res = svc.build_by_strategy(STRATEGY, holding_days=5, today="2026-10-01")
    assert res["days_tracked"] == 30
    assert res["first_signal_at"] == "2026-09-01 10:00:00"
    assert res["mature_count"] == 1
    assert res["pending_count"] == 1


def test_no_signals_returns_zero_without_error(stub):
    """⑦ 该策略零信号 → 全 0/None，不抛异常。"""
    stub["signals"] = []
    stub["klines"] = {}
    res = svc.build_by_strategy(STRATEGY, holding_days=5, today="2026-10-01")
    assert res["strategy_id"] == STRATEGY
    assert res["signal_count"] == 0
    assert res["mature_count"] == 0
    assert res["pending_count"] == 0
    assert res["holding_days"] == 5
    assert res["win_rate"] is None
    assert res["wilson_ci"] == [0.0, 1.0]
    assert res["avg_return"] is None
    assert res["expectancy"] is None
    assert res["payoff"] is None
    assert res["days_tracked"] == 0
    assert res["first_signal_at"] == ""
    assert res["signals"] == []

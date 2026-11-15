"""夏普「算不出来」必须是未知，不能静默变成 0。

真实数据（SR-20261014-01F173 第 1 轮）：OOS 净值曲线 143 个日收益、均值 -0.056%、标准差 1.07%，
手算夏普约 -0.05（亏损），但引擎报 **0** —— 因为 backtrader 的 SharpeRatio 在算不出来时返回
None，而 `round(x) if x else 0` 把它变成了「夏普 = 0（中性）」。

两个后果都很实在：
① 亏损策略在面板/卡片上看起来夏普中性；
② 研发选优把「算不出来」当 0 分，反而压过真正算出来的负夏普 → 亏损策略当选 best。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import _calculate_stats
from backtest.strategy_research import IterRecord, StrategyResearchLoop


def _stats(sharpe):
    return _calculate_stats(
        sharpe=sharpe, drawdown={}, returns={}, trade_analysis={}, time_return={},
        initial_cash=1_000_000, final_value=900_000, start_date='', end_date='',
    )


def test_sharpe_none_stays_none():
    assert _stats({'sharperatio': None})['sharpe_ratio'] is None


def test_sharpe_number_is_kept():
    assert _stats({'sharperatio': -1.42713})['sharpe_ratio'] == -1.4271
    assert _stats({'sharperatio': 0.5})['sharpe_ratio'] == 0.5


def _iter(round_no, oos_sharpe, **kw):
    rec = IterRecord(round=round_no, hypothesis='h', strategy_json='{}')
    rec.compile_ok = True
    rec.oos_metrics = {} if oos_sharpe is None else {'sharpe': oos_sharpe, 'trades': 30}
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


def test_unknown_sharpe_never_wins_selection():
    """「算不出来」不能压过「算出来是负的」——否则亏损策略会当选 best"""
    loop = StrategyResearchLoop
    iters = [_iter(1, None), _iter(2, -1.4), _iter(3, -2.2)]
    best = loop.select_best(None, iters)          # 只用到类属性 OVERFIT_RATIO
    assert best is not None and best.round == 2, best.round


def test_all_unknown_means_no_best():
    assert StrategyResearchLoop.select_best(None, [_iter(1, None), _iter(2, None)]) is None


def test_overfit_flag_still_excluded():
    iters = [_iter(1, 2.0, overfit_flag=1), _iter(2, -0.5)]
    assert StrategyResearchLoop.select_best(None, iters).round == 2


# ============================================================
# 年化必须按自然日，不能按 bar 数（事故 2026-10-18）
# ============================================================
# 16 根月线、总收益 10.52%：旧口径 annual_return = rnorm100/100 = 3.8306（383%），
# 因为 backtrader 固定按 252 期/年放大，把 16 根 bar 当成 16 个交易日。

def _stats_with_span(total_return, bars, span_days):
    time_return = {f'd{i}': 0.0 for i in range(bars)}
    return _calculate_stats(
        sharpe={}, drawdown={}, returns={'rnorm100': 383.06},
        trade_analysis={}, time_return=time_return,
        initial_cash=1_000_000, final_value=1_000_000 * (1 + total_return),
        start_date='2023-10-19', end_date='2026-10-18', span_days=span_days,
    )


def test_monthly_bars_do_not_get_annualized():
    """16 根跨 1080 天 = 非日线 → 年化 None，并标记 bar 频率异常。"""
    st = _stats_with_span(0.1052, 16, 1080)
    assert st['annual_return'] is None
    assert st['bar_frequency_abnormal'] == 1


def test_daily_bars_use_calendar_cagr():
    """730 根日线跨 1095 天：cagr = (1+0.21)^(365/1095)-1 ≈ 6.56%。"""
    st = _stats_with_span(0.21, 730, 1095)
    assert st['annual_return'] == pytest.approx(0.0656, abs=1e-4)
    assert st['bar_frequency_abnormal'] == 0


def test_no_span_keeps_legacy_behaviour():
    """拿不到跨度（老调用/单测）时维持旧口径，不把年化变成 None。"""
    st = _stats_with_span(0.21, 730, 0)
    assert st['annual_return'] == pytest.approx(3.8306, abs=1e-4)

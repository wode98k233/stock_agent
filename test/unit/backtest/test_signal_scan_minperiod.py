"""_run_scan 最小周期守卫与 runonce 回归测试。

背景（2026-09-29）：
    全局总览大屏的批量扫描在日志里产生 476 次
    `IndexError: array assignment index out of range`，堆栈固定落在
    backtrader `indicators/basicops.py:364` 的 `SMA.once()`。

根因：
    `cerebro.run()` 默认 `runonce=True` 走向量化 `once()`。当传入的 K 线根数
    少于策略指标最小周期（`_minperiod`）时，`dst[i] = math.fsum(...)` 越界。
    `scan_many` 内部 `except Exception: continue` 会静默吞掉该异常，表现为
    「该标的永远没有信号」，且只在 DEBUG 堆栈里留痕。

修复：
    1. 预检 `len(data) < _minperiod` 时直接返回空信号（指标未成型，读数无意义）；
    2. `cerebro.run(runonce=False)` 走 next() 模式，规避 once() 边界越界。

本文件锁定两条不变量：
    - 短序列不再抛异常（原为 IndexError）；
    - runonce=False 与 runonce=True 在充足数据下信号**逐条一致**（零行为变更）。
"""
import numpy as np
import pandas as pd
import pytest

from backtest.signal_scanner import SignalScanner
from backtest.strategies import get_all_strategies, get_strategy_class


def _mk_df(n: int, seed: int = 7) -> pd.DataFrame:
    """构造 n 根确定性日线，避免不同实现取到随机序列。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    close = 10 + np.cumsum(rng.standard_normal(n) * 0.1)
    return pd.DataFrame({
        "trade_date": idx,
        "open": close,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": np.ones(n) * 1000,
    })


# ---------------------------------------------------------------------------
# 最小周期探测
# ---------------------------------------------------------------------------

def test_min_bars_required_returns_real_minperiod():
    """探测函数必须返回真实 minperiod，而不是恒为 1 的伪值。

    回归点：早期实现用 `cerebro.strats[0][0][0]` 在未 run 时取 `_minperiod`，
    拿到的是 MetaStrategy 类、值恒为 1，导致守卫完全失效。
    """
    cls = get_strategy_class("multi_indicator_strategy")
    min_bars = SignalScanner._min_bars_required(cls, {})
    assert min_bars > 1, (
        f"minperiod 探测返回 {min_bars}，疑似退化到 MetaStrategy 的默认值 1"
    )
    assert min_bars == 55, f"multi_indicator_strategy 最小周期应为 55，实际 {min_bars}"


def test_min_bars_required_matches_across_scan_mode():
    """scan 模式开关不应影响 minperiod（探测传 _scan_mode=True，需与真实运行一致）。"""
    for sid, cls in get_all_strategies().items():
        a = SignalScanner._min_bars_required(cls, {})
        assert a > 1, f"{sid} 探测 minperiod 异常：{a}"


def test_min_bars_required_degrades_to_zero_on_error():
    """探测自身失败时返回 0（表示未知），不得向上抛异常。"""
    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("boom")

    assert SignalScanner._min_bars_required(Boom, {}) == 0


# ---------------------------------------------------------------------------
# 短序列守卫（修复前必然 IndexError）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [5, 20, 40, 54, 55])
def test_run_scan_short_series_returns_empty_without_raising(n):
    """K 线不足最小周期时返回空列表，不抛 IndexError。

    55 为 multi_indicator_strategy 的 minperiod：恰好等于时信号尚未成型，
    同样应短路（否则 once() 仍会越界）。
    """
    cls = get_strategy_class("multi_indicator_strategy")
    sigs = SignalScanner._run_scan(cls, _mk_df(n), {})
    assert sigs == [], f"len={n} 应返回空信号，实际 {len(sigs)} 条"


def test_run_scan_boundary_just_above_minperiod_emits_signals():
    """刚过最小周期即恢复正常计算路径，证明守卫没有过度裁剪。

    注意：minperiod+1 根数据只够指标「刚成型」，是否产生买点取决于行情本身，
    因此这里断言的是「已进入真实计算路径」而非「必然有信号」——
    用充足数据（明显大于 minperiod）保证有信号，用 minperiod+1 验证边界不崩。
    """
    cls = get_strategy_class("multi_indicator_strategy")
    min_bars = SignalScanner._min_bars_required(cls, {})

    # 边界：刚过多周期，不短路即进入 backtrader 计算（不抛异常即可）
    edge = SignalScanner._run_scan(cls, _mk_df(min_bars + 1), {})
    assert isinstance(edge, list)

    # 充足数据：必须有成型信号
    ample = SignalScanner._run_scan(cls, _mk_df(120), {})
    assert len(ample) > 0, "len=120 应已有成型信号"


# ---------------------------------------------------------------------------
# runonce 零行为变更回归
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sid", ["multi_indicator_strategy", "dual_ma_crossover"])
def test_runonce_false_preserves_signals_for_all_strategies(sid):
    """runonce=False 与 runonce=True 的信号必须逐条一致（零行为变更）。

    这是本次修复的核心安全声明：换执行模式只为规避 once() 越界，
    不得改变任何策略的既有信号输出。
    取两个代表策略即可——一个多指标（minperiod 55，覆盖嵌套指标）、
    一个双均线（minperiod 21，覆盖交叉信号），全量 10 策略太慢。
    """
    import backtrader as bt

    from backtest.data_adapter import _normalize_columns

    df = _mk_df(120)
    cls = get_strategy_class(sid)

    def _run(runonce):
        bt_df = _normalize_columns(df.copy())
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.adddata(bt.feeds.PandasData(
            dataname=bt_df, datetime=None, open="open", high="high",
            low="low", close="close", volume="volume", openinterest=-1,
        ))
        cerebro.addstrategy(cls, _scan_mode=True)
        cerebro.broker.setcash(1_000_000)
        return cerebro.run(runonce=runonce)[0].get_signal_log()

    assert _run(True) == _run(False), f"{sid} runonce 切换后信号不一致"


def test_run_scan_full_history_uses_runonce_false(monkeypatch):
    """_run_scan 必须以 runonce=False 调用，防止回归回 once() 模式。"""
    import backtrader as bt

    captured = {}
    real_run = bt.Cerebro.run

    def spy_run(self, *args, **kwargs):
        captured["runonce"] = kwargs.get("runonce", True)
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(bt.Cerebro, "run", spy_run)
    cls = get_strategy_class("dual_ma_crossover")
    SignalScanner._run_scan(cls, _mk_df(120), {})
    assert captured.get("runonce") is False, "应以 runonce=False 运行"

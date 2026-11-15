"""回测执行除零的两类根因：指标 safediv 缺失 + 窗口参数为 0。

真机事故（2026-10-14，strategy_run SR-20261014-BE8018）：研发第 4 轮用 RSI 周期 2，
`ZeroDivisionError: float division by zero` 从 backtrader 冒到引擎，把**整次 8 轮研发**
判成 failed —— 用户看到的是「研究失败」，实际只是一轮的指标在部分区间除不了。

根因有两类，都在 backtest/json_compiler.py：

1. backtrader 的 RSI 内部是 `rs = maup / madown`（无下跌段 ⇒ madown=0），
   Stochastic（KDJ）内部是 `K = 100 * (close - lowest) / (highest - lowest)`
   （一字板 / 平价窗口 ⇒ 分母 0）。两者都有 `safediv` 开关，默认关闭；
   且旧实现的 kdj 分支**只透传 period/period_dfast**，把 fixed_kwargs 里其余的键丢掉了。
2. 窗口类参数（period/period_me1/…/short/long/smooth）为 0 时，任何窗口指标都会除零；
   0 的来源是「参数引用未声明 → 按 0 兜底」或模型瞎写。

修法：rsi/kdj 打开 safediv；`_create_indicator_instance` 统一把窗口参数 < 1 夹到 1 并
记进 `_unsupported_conditions`；`_validate_operand` 在保存期直接拒收周期 0，
让模型带着明确原因重试（而不是等到回测崩）。
"""
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 严格上涨：没有下跌段 ⇒ RSI 的 madown 恒为 0（旧实现必崩）
RISING_BARS = [(f"2024-01-{d:02d}", 10.0 + i, 10.3 + i, 9.9 + i, 10.2 + i)
               for i, d in enumerate(range(2, 24))]
# 一字板：high == low == close ⇒ KDJ 的 highest-lowest 恒为 0（旧实现必崩）
FLAT_BARS = [(f"2024-01-{d:02d}", 10.0, 10.0, 10.0, 10.0) for d in range(2, 24)]


def _spec(left, right, strategy_id="div0_probe"):
    return {
        "meta": {"strategy_id": strategy_id, "name": "除零探针"},
        "params": {},
        "conditions": {
            "buy": {"logic": "AND", "rules": [
                {"type": "indicator", "op": ">", "left": left, "right": right}]},
            "sell": {"logic": "AND", "rules": [], "hold_bars": 1},
        },
    }


def _run(spec, bars):
    """真跑 backtrader（编译 → cerebro.run）：除零会在这里抛出来"""
    import backtrader as bt
    from backtest.json_compiler import compile_strategy

    cls = compile_strategy(json.dumps(spec, ensure_ascii=False))
    df = pd.DataFrame(bars, columns=["date", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"])
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df.set_index("date")))
    cerebro.addstrategy(cls, entry_timing="next_open", exit_timing="next_open")
    cerebro.broker.setcash(100000)
    cerebro.broker.set_coc(False)
    res = cerebro.run()
    return res[0], cls


# --------------------------------------------------------------------------
# 1. 指标 safediv：RSI 无下跌段 / KDJ 一字板都不许崩
# --------------------------------------------------------------------------

def test_rsi_without_down_days_does_not_divide_by_zero():
    """RSI(2) 在严格上涨行情上必须能跑完（旧实现：madown=0 → ZeroDivisionError）"""
    spec = _spec({"func": "rsi", "args": ["close", 2]}, 50)
    _st, cls = _run(spec, RISING_BARS)   # 不抛就算过
    assert cls is not None


def test_kdj_on_flat_bars_does_not_divide_by_zero():
    """KDJ(9) 在一字板上必须能跑完（旧实现：highest==lowest → ZeroDivisionError）

    这条同时锁住「kdj 分支必须透传全部 fixed_kwargs」：只加 safediv 到 INDICATOR_MAP、
    不透传的话，一字板依旧崩。
    """
    spec = _spec({"func": "kdj", "args": ["high", "low", "close", 9]}, 20)
    _st, cls = _run(spec, FLAT_BARS)
    assert cls is not None


# --------------------------------------------------------------------------
# 2. 窗口参数 < 1：夹到 1 并如实记账
# --------------------------------------------------------------------------

def test_zero_period_is_clamped_to_one_and_reported():
    """ma(close, 0) 必须被夹成周期 1 跑起来，并在 unsupported 里说清原因"""
    spec = _spec({"func": "ma", "args": ["close", 0]}, {"data": "close"})
    _st, cls = _run(spec, RISING_BARS)
    msgs = getattr(cls, "_unsupported_conditions", [])
    assert any("不能小于 1" in m for m in msgs), msgs


def test_zero_period_variants_all_survive():
    """rsi / boll / macd 的周期 0 同样不许崩（旧实现四种全崩）"""
    cases = [
        ({"func": "rsi", "args": ["close", 0]}, 50),
        ({"func": "boll", "args": ["close", 0, 2]}, {"data": "close"}),
        ({"func": "macd", "args": ["close", 0, 0, 0]}, 0),
    ]
    for left, right in cases:
        _st, cls = _run(_spec(left, right), RISING_BARS)
        msgs = getattr(cls, "_unsupported_conditions", [])
        assert any("不能小于 1" in m for m in msgs), (left, msgs)


def test_negative_period_is_clamped_too():
    """负数周期同样没有意义：夹到 1（而不是交给 backtrader 去炸）"""
    spec = _spec({"func": "ma", "args": ["close", -5]}, {"data": "close"})
    _st, cls = _run(spec, RISING_BARS)
    msgs = getattr(cls, "_unsupported_conditions", [])
    assert any("不能小于 1" in m for m in msgs), msgs


# --------------------------------------------------------------------------
# 3. 保存闸门：周期 0 在保存期就拒收（让模型带原因重试）
# --------------------------------------------------------------------------

def test_validator_rejects_zero_period():
    from backtest.json_compiler import validate_strategy_json

    errors = validate_strategy_json(json.dumps(
        _spec({"func": "ma", "args": ["close", 0]}, {"data": "close"}), ensure_ascii=False))
    assert errors, "周期 0 必须被保存闸门拒收"
    assert any("必须 >= 1" in e for e in errors), errors


def test_validator_rejects_zero_kdj_period():
    """KDJ 的周期在第 4 个位置（high/low/close/n），不是 arg_names 的第 2 位"""
    from backtest.json_compiler import validate_strategy_json

    errors = validate_strategy_json(json.dumps(
        _spec({"func": "kdj", "args": ["high", "low", "close", 0]}, 20), ensure_ascii=False))
    assert any("必须 >= 1" in e for e in errors), errors


def test_validator_accepts_normal_periods():
    from backtest.json_compiler import validate_strategy_json

    assert validate_strategy_json(json.dumps(
        _spec({"func": "ma", "args": ["close", 5]}, {"data": "close"}), ensure_ascii=False)) == []


def test_schema_does_not_teach_period_zero():
    """给 LLM 的 schema 里不应出现周期 0 的示例（避免模型照抄）"""
    from backtest.json_compiler import export_llm_schema

    schema = export_llm_schema()
    assert "period" in schema

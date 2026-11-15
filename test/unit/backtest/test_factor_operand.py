"""因子操作数（S4 · F5-B）单测。

设计要点：``{"factor": "<field>"}`` 做成**数据线**而不是 Backtrader Indicator
包装——后者每根 bar 重算一次全序列，几百根就 O(n²) 爆掉。这里锁三件事：
1. 编译器登记 factor_fields（引擎据此给数据源补线）；
2. 未知因子按**恒假**处理并记账（不能静默通过、也不能崩）；
3. 数据源上的 f_<field> 列与 ``compute_factors`` 逐点一致（值错的话
   「回测能跑」也说明不了任何问题）。
"""
import json

import pytest


def _strategy(left: dict, op: str = "<", right: dict | None = None) -> str:
    return json.dumps({
        "meta": {"strategy_id": "t", "name": "t"},
        "params": {},
        "conditions": {
            "buy": {"logic": "AND", "rules": [
                {"type": "indicator", "left": left, "op": op,
                 "right": right or {"value": 0.3}},
            ]},
            "sell": {"logic": "AND", "hold_bars": 5, "rules": []},
        },
        "chart_indicators": [],
    }, ensure_ascii=False)


def test_factor_operand_registers_fields():
    from backtest.json_compiler import compile_strategy
    cls = compile_strategy(_strategy({"factor": "close_position_20"}))
    assert set(cls._factor_fields) == {"close_position_20"}
    assert not [u for u in cls._unsupported_conditions if "因子" in u]


def test_two_factors_in_one_strategy():
    from backtest.json_compiler import compile_strategy
    cls = compile_strategy(_strategy({"factor": "rsi_14"}, right={"value": 30}))
    assert set(cls._factor_fields) == {"rsi_14"}


def test_unknown_factor_is_const_false_and_recorded():
    """未知因子必须按恒假 + 记账，不能静默通过、更不能崩。"""
    from backtest.json_compiler import compile_strategy
    cls = compile_strategy(_strategy({"factor": "no_such_factor"}))
    assert cls._factor_fields == frozenset()
    assert any("no_such_factor" in u for u in cls._unsupported_conditions)


def test_meta_exposes_factors():
    from backtest.json_compiler import get_strategy_meta
    factors = get_strategy_meta()["factors"]
    assert len(factors) >= 30
    first = factors[0]
    assert {"field", "label", "description"} <= set(first)


def test_strategy_without_factor_registers_nothing():
    """回归：不含因子的老策略不受影响（引擎会传空集合）。"""
    from backtest.json_compiler import compile_strategy
    cls = compile_strategy(_strategy({"data": "close"}, right={"func": "ma", "args": ["close", 20]}))
    assert cls._factor_fields == frozenset()


# ── 数据源侧（依赖真实行情，缺失则跳过）─────────────────────

def _has_data():
    try:
        from backtest.data_adapter import _load_stock_daily
        df = _load_stock_daily('600519', '2025-01-01', '2026-01-01', auto_heal=False)
        return not df.empty
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _has_data(), reason="库内无 600519 行情数据")
def test_feed_carries_factor_line_matching_lib():
    import numpy as np
    from backtest.data_adapter import BacktraderDataFeed
    from backtest.factor_lib import compute_factors

    feed = BacktraderDataFeed.from_stock_daily(
        '600519', '2025-01-01', '2026-01-01', factor_fields=['close_position_20'])
    df = feed.p.dataname
    assert 'f_close_position_20' in df.columns
    expect = compute_factors(df, ['close_position_20'])['close_position_20']
    got = df['f_close_position_20']
    assert np.allclose(np.nan_to_num(got.to_numpy()), np.nan_to_num(expect.to_numpy()))
    assert got.notna().sum() > 0, "因子线全是 NaN（预热期应只有开头几根）"


@pytest.mark.skipif(not _has_data(), reason="库内无 600519 行情数据")
def test_feed_without_factor_fields_unchanged():
    """老路径不带因子时，feed 仍是原类（不引入多余 line）。"""
    from backtest.data_adapter import AStockPandasData, BacktraderDataFeed
    feed = BacktraderDataFeed.from_stock_daily('600519', '2025-01-01', '2026-01-01')
    assert isinstance(feed, AStockPandasData)
    assert type(feed) is AStockPandasData


# ── 前端接线 ──────────────────────────────────────────────

def test_builder_supports_factor_operand():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[3] / "server/static/js/backtest.js").read_text(
        encoding="utf-8")
    assert "if (fd.type === 'factor') return { factor:" in js, "序列化缺 factor 分支"
    assert "if (def.factor) return { type: 'factor'" in js, "反序列化缺 factor 分支"
    assert "_builderChangeValFactor" in js
    assert '<option value="factor"' in js, "构造器缺「因子」类型选项"

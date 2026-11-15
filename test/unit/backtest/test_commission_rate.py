"""A 股佣金口径回归测试。

背景（2026-11-06）：`AStockCommission` 传的是小数比率（0.0003 = 万三），
但 backtrader 在 COMM_PERC 下默认把 commission 当百分数再除以 100
（`CommInfoBase.__init__`: `if commtype == PERC and not percabs: commission /= 100`）。
结果万三变成十万分之三，每笔佣金都落到最低 5 元——实测 98.8 万成交额只记 14.88 元。
修复方式是打开 `percabs=True`，让费率按传入的小数比率原样使用。
"""
from __future__ import annotations

import pytest

from backtest.a_stock_broker import AStockCommission


def _comm(**kwargs):
    return AStockCommission(**kwargs)


def test_commission_amount_is_not_divided_by_100():
    """万三的小数比率必须原样生效，不能被 backtrader 再除以 100。"""
    ci = _comm()
    assert ci.p.commission == pytest.approx(0.0003), (
        f"commission 参数被改成了 {ci.p.commission!r}，"
        "说明 percabs 没生效（backtrader 会除以 100）"
    )


def test_buy_commission_matches_by_hand():
    """买入：佣金 296.38 + 过户费 9.88 = 306.26（700 股 × 1411.35）。"""
    ci = _comm()
    assert ci.getcommission(700, 1411.35) == pytest.approx(306.26, abs=0.05)


def test_sell_commission_includes_stamp_tax():
    """卖出：佣金 + 印花税（万五）+ 过户费 = 800.24。"""
    ci = _comm()
    assert ci.getcommission(-700, 1411.35) == pytest.approx(800.24, abs=0.05)


def test_etf_has_no_stamp_tax():
    """ETF 免印花税：卖出只多过户费，和买入一致。"""
    ci = _comm(stamp_tax=0)
    assert ci.getcommission(-700, 1411.35) == pytest.approx(306.26, abs=0.05)


def test_min_commission_applies_to_tiny_trade():
    """小额交易落到 5 元最低佣金。"""
    ci = _comm()
    # 100 股 × 1 元 = 100 元成交额，万三只有 0.03 元 → 取最低 5 元 + 过户费
    assert ci.getcommission(100, 1.0) == pytest.approx(5.0 + 0.001, abs=0.01)

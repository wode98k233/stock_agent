"""回测口径三处修复的回归测试（2026-11-07）。

背景：两个独立复核（辩护/质疑）确认——
1. `save_backtest_trades` 只 INSERT 不 DELETE，同一 run 重跑时成交记录成批叠加
   （实测某 run `trade_count=29`，库里却有 55 买 55 卖），会污染一切按成交重算的指标。
2. `win_rate > 1` 的 65 行全部恰好等于 `win_count`（旧公式 `won/effective(=1)` 的假象），
   `_win_rate_fraction` 盲除 100 会把它显示成「23%」（真实 50.56%）。
3. 详情页把 `win_rate = null` 折成 `0.0%`，而列表页正确显示 `—`。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def bt_db(tmp_path, monkeypatch):
    """回测库指向临时文件，绝不碰 db/backtest.db。"""
    from config import Config
    test_db = str(tmp_path / "backtest.db")
    monkeypatch.setattr(Config, "get_backtest_db_path", lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    return test_db


def _trade(no: int, direction: str = "buy") -> dict:
    return {"trade_no": no, "direction": direction, "trade_date": "2026-03-02",
            "price": 10.0, "quantity": 100, "amount": 1000.0,
            "commission": 5.0, "slippage_cost": 1.0, "total_cost": 5.0}


def test_save_trades_is_idempotent_per_run(bt_db):
    """同一个 run 存两次 → 不应翻倍（重跑不叠加）。"""
    from utils.cache.backtest_db import get_backtest_trades, save_backtest_trades
    trades = [_trade(1), _trade(2, "sell")]
    save_backtest_trades("RUN-A", trades)
    save_backtest_trades("RUN-A", trades)
    assert len(get_backtest_trades("RUN-A")) == 2, "重跑把成交记录存了两遍"


def test_save_trades_keeps_other_runs(bt_db):
    """删的是本 run 的行，别的 run 不能被误伤。"""
    from utils.cache.backtest_db import get_backtest_trades, save_backtest_trades
    save_backtest_trades("RUN-A", [_trade(1), _trade(2, "sell")])
    save_backtest_trades("RUN-B", [_trade(1)])
    save_backtest_trades("RUN-A", [_trade(1)])
    assert len(get_backtest_trades("RUN-B")) == 1
    assert len(get_backtest_trades("RUN-A")) == 1


class TestWinRateFraction:
    """`>1` 要区分：旧算式假象 vs 历史百分数刻度。"""

    def test_normal_fraction_passthrough(self):
        from server.routes.backtest import _win_rate_fraction
        assert _win_rate_fraction(0.55) == pytest.approx(0.55)

    def test_legacy_percent_without_win_count_still_normalized(self):
        from server.routes.backtest import _win_rate_fraction
        assert _win_rate_fraction(9.0) == pytest.approx(0.09)

    def test_equals_win_count_is_artifact_not_usable(self):
        """23.0 且 win_count=23 → 旧公式 won/1 的假象，返回 None。"""
        from server.routes.backtest import _win_rate_fraction
        assert _win_rate_fraction(23.0, 23) is None
        assert _win_rate_fraction(111.0, 111) is None

    def test_over_one_but_not_win_count_is_percent(self):
        from server.routes.backtest import _win_rate_fraction
        assert _win_rate_fraction(55.0, 12) == pytest.approx(0.55)

    def test_service_helper_matches(self):
        from server.backtest_service import _win_rate_fraction as svc
        assert svc(23.0, 23) is None
        assert svc(9.0) == pytest.approx(0.09)
        assert svc(None) is None

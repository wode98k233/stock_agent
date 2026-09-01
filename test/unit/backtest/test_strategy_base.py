"""Test: StockSolveStrategy 基类核心逻辑

覆盖：仓位计算、T+1 检查、止损止盈。
使用 mock 替代 Backtrader 引擎，纯单元测试。
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import date
from backtest.strategy_base import StockSolveStrategy


def _make_strategy(cash=100000, price=10.0, position_mode='full',
                   position_size=100, lot_size=100, t_plus_1=True,
                   has_position=False, position_size_val=0,
                   buy_price=None, buy_date=None, highest_since_buy=0,
                   stop_loss=0, take_profit=0, trailing_stop=0,
                   current_price=None):
    """创建 mock 策略实例，patch Backtrader 的 property"""
    strat = StockSolveStrategy.__new__(StockSolveStrategy)

    # params
    strat.p = MagicMock()
    strat.p.position_mode = position_mode
    strat.p.position_size = position_size
    strat.p.lot_size = lot_size
    strat.p.t_plus_1 = t_plus_1
    strat.p.stop_loss = stop_loss
    strat.p.take_profit = take_profit
    strat.p.trailing_stop = trailing_stop

    # broker
    strat.broker = MagicMock()
    strat.broker.getcash.return_value = cash

    # data
    strat.data = MagicMock()
    strat.data.close = [current_price if current_price is not None else price]
    strat.data.datetime = MagicMock()
    strat.data.datetime.date.return_value = date(2025, 1, 3)

    # trade state
    strat.order = None
    strat.buy_price = buy_price
    strat.buy_date = buy_date
    strat.highest_since_buy = highest_since_buy
    strat._pending_signal_reason = None
    strat.sell_with_reason = MagicMock()

    # position: 通过 patch property 方式设置
    mock_pos = MagicMock()
    mock_pos.size = position_size_val if has_position else 0
    strat._mock_position = mock_pos

    return strat


@pytest.fixture(autouse=True)
def _patch_position():
    """自动 patch StockSolveStrategy.position 为可写的 mock"""
    with patch.object(StockSolveStrategy, 'position', new_callable=PropertyMock) as prop:
        yield prop


# ── 仓位计算 _calc_buy_size ──

class TestCalcBuySize:

    def test_full_mode(self, _patch_position):
        strat = _make_strategy(cash=100000, price=10.0, position_mode='full')
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 9900

    def test_percent_mode(self, _patch_position):
        strat = _make_strategy(cash=100000, price=10.0, position_mode='percent', position_size=50)
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 5000

    def test_fixed_mode(self, _patch_position):
        strat = _make_strategy(cash=100000, price=10.0, position_mode='fixed', position_size=3000)
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 300

    def test_zero_price_returns_zero(self, _patch_position):
        strat = _make_strategy(cash=100000, price=0)
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 0

    def test_negative_price_returns_zero(self, _patch_position):
        strat = _make_strategy(cash=100000, price=-5)
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 0

    def test_lot_size_rounding(self, _patch_position):
        strat = _make_strategy(cash=1500, price=10.0, position_mode='full')
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 100

    def test_exact_one_lot(self, _patch_position):
        strat = _make_strategy(cash=1000, price=10.0, position_mode='full')
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 100

    def test_insufficient_still_returns_lot(self, _patch_position):
        strat = _make_strategy(cash=500, price=10.0, position_mode='full')
        _patch_position.return_value = strat._mock_position
        assert strat._calc_buy_size() == 100


# ── T+1 检查 ──

class TestTPlus1:

    def test_no_buy_date_allows_sell(self, _patch_position):
        strat = _make_strategy(buy_date=None)
        assert strat._check_t_plus_1() is True

    def test_disabled_allows_sell(self, _patch_position):
        strat = _make_strategy(t_plus_1=False, buy_date=date(2025, 1, 3))
        assert strat._check_t_plus_1() is True

    def test_same_day_blocks_sell(self, _patch_position):
        strat = _make_strategy(buy_date=date(2025, 1, 3))
        assert strat._check_t_plus_1() is False

    def test_next_day_allows_sell(self, _patch_position):
        strat = _make_strategy(buy_date=date(2025, 1, 2))
        assert strat._check_t_plus_1() is True


# ── 止损止盈 ──

class TestStopLoss:

    def test_triggers_at_threshold(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=95, stop_loss=5,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 1))
        _patch_position.return_value = strat._mock_position
        strat._check_stop_loss()
        strat.sell_with_reason.assert_called_once()

    def test_not_triggered(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=96, stop_loss=5,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 1))
        _patch_position.return_value = strat._mock_position
        strat._check_stop_loss()
        strat.sell_with_reason.assert_not_called()

    def test_t_plus_1_blocks(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=90, stop_loss=5,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 3), t_plus_1=True)
        _patch_position.return_value = strat._mock_position
        strat._check_stop_loss()
        strat.sell_with_reason.assert_not_called()

    def test_trailing_stop(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=90,
                               trailing_stop=10, highest_since_buy=100,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 1))
        _patch_position.return_value = strat._mock_position
        strat._check_stop_loss()
        strat.sell_with_reason.assert_called_once()

    def test_no_position_skips(self, _patch_position):
        strat = _make_strategy(has_position=False, current_price=90, stop_loss=5)
        _patch_position.return_value = strat._mock_position
        strat._check_stop_loss()
        strat.sell_with_reason.assert_not_called()

    def test_zero_disabled(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=50, stop_loss=0,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 1))
        _patch_position.return_value = strat._mock_position
        strat._check_stop_loss()
        strat.sell_with_reason.assert_not_called()


class TestTakeProfit:

    def test_triggers(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=110, take_profit=10,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 1))
        _patch_position.return_value = strat._mock_position
        strat._check_take_profit()
        strat.sell_with_reason.assert_called_once()

    def test_not_triggered(self, _patch_position):
        strat = _make_strategy(buy_price=100, current_price=109, take_profit=10,
                               has_position=True, position_size_val=100,
                               buy_date=date(2025, 1, 1))
        _patch_position.return_value = strat._mock_position
        strat._check_take_profit()
        strat.sell_with_reason.assert_not_called()

"""StockSolve 策略基类

所有内置和用户自定义策略都继承此基类。
提供：
- 统一的交易记录
- A股规则集成（T+1、100股最小单位）
- 止损止盈逻辑
- 仓位管理
- 参数说明（供前端渲染表单）
"""
import backtrader as bt
from datetime import datetime
from typing import Dict, Any, List, Optional


class StockSolveStrategy(bt.Strategy):
    """Stock Radar 策略基类

    子类只需实现 next() 方法，基类提供：
    - notify_order / notify_trade 统一处理
    - A股 T+1 规则（当天买入次日才能卖出）
    - 最小交易单位 100 股
    - 止损止盈（固定/移动）
    - 仓位管理（全仓/固定比例/固定金额）
    """

    params = (
        ('stop_loss', 0),        # 止损百分比 (0=不启用)
        ('take_profit', 0),      # 止盈百分比 (0=不启用)
        ('trailing_stop', 0),    # 移动止损百分比 (0=不启用)
        ('position_mode', 'full'),  # full / percent / fixed
        ('position_size', 1.0),  # 仓位比例或固定金额
        ('t_plus_1', True),      # T+1 规则
        ('lot_size', 100),       # 最小交易单位
        # 信号扫描模式：True 时 on_bar 只逐 bar 记录买卖条件命中，不实际下单
        ('_scan_mode', False),
    )

    def __init__(self):
        self.order = None
        self.buy_price = None
        self.buy_date = None
        self.highest_since_buy = 0
        self.trade_records: List[Dict[str, Any]] = []
        self._trade_no = 0
        self._pending_signal_reason = None
        # scan 模式下逐 bar 记录买卖条件命中（无状态评估）
        self._signal_log: List[Dict[str, Any]] = []

    # -----------------------------------------------------------
    # 交易执行辅助方法
    # -----------------------------------------------------------

    def buy_with_reason(self, reason: str = '', **kwargs):
        """买入（带信号原因）"""
        if self.order:
            return  # 有未完成订单，跳过
        self._pending_signal_reason = reason
        size = self._calc_buy_size()
        if size > 0:
            self.order = self.buy(size=size, **kwargs)

    def sell_with_reason(self, reason: str = '', **kwargs):
        """卖出（带信号原因）"""
        if self.order:
            return
        if not self.position:
            return
        self._pending_signal_reason = reason
        self.order = self.sell(size=self.position.size, **kwargs)

    def _calc_buy_size(self) -> int:
        """计算买入数量（考虑仓位和最小交易单位）

        A股规则：最小交易单位为 100 股（1 手）。
        当资金不足以买 1 手时，尝试买 1 手（允许透支，由 broker 拒绝）。
        """
        cash = self.broker.getcash()
        price = self.data.close[0]
        if price <= 0:
            return 0

        if self.p.position_mode == 'full':
            target_cash = cash * 0.99
        elif self.p.position_mode == 'percent':
            target_cash = cash * self.p.position_size / 100
        elif self.p.position_mode == 'fixed':
            target_cash = min(self.p.position_size, cash)
        else:
            target_cash = cash * 0.99

        size = int(target_cash / price)
        lot = self.p.lot_size

        if lot > 0:
            if size >= lot:
                # 正常情况：取整到 lot 的倍数
                size = (size // lot) * lot
            elif cash >= price * lot:
                # 资金刚好够 1 手（取整后为 0，但实际买得起）
                size = lot
            else:
                # 资金不够 1 手，尝试买 1 手（broker 会拒绝余额不足的订单）
                size = lot

        return max(size, 0)

    def _check_t_plus_1(self) -> bool:
        """检查 T+1 规则：当天买入不能当天卖"""
        if not self.p.t_plus_1:
            return True
        if self.buy_date is None:
            return True
        current_date = self.data.datetime.date(0)
        return current_date > self.buy_date

    # -----------------------------------------------------------
    # 止损止盈检查
    # -----------------------------------------------------------

    def _check_stop_loss(self):
        """检查止损条件"""
        if not self.position or self.buy_price is None:
            return
        if not self._check_t_plus_1():
            return

        current_price = self.data.close[0]
        pnl_pct = (current_price - self.buy_price) / self.buy_price * 100

        # 固定止损
        if self.p.stop_loss > 0 and pnl_pct <= -self.p.stop_loss:
            self.sell_with_reason(f'止损 {self.p.stop_loss}%')
            return

        # 移动止损
        if self.p.trailing_stop > 0 and self.highest_since_buy > 0:
            drop_from_high = (self.highest_since_buy - current_price) / self.highest_since_buy * 100
            if drop_from_high >= self.p.trailing_stop:
                self.sell_with_reason(f'移动止损 从高点回落 {drop_from_high:.1f}%')
                return

    def _check_take_profit(self):
        """检查止盈条件"""
        if not self.position or self.buy_price is None:
            return
        if not self._check_t_plus_1():
            return

        if self.p.take_profit > 0:
            current_price = self.data.close[0]
            pnl_pct = (current_price - self.buy_price) / self.buy_price * 100
            if pnl_pct >= self.p.take_profit:
                self.sell_with_reason(f'止盈 {self.p.take_profit}%')

    # -----------------------------------------------------------
    # Backtrader 回调
    # -----------------------------------------------------------

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        if order.status == order.Completed:
            if order.isbuy():
                self.buy_price = order.executed.price
                self.buy_date = self.data.datetime.date(0)
                self.highest_since_buy = order.executed.price
                # 记录买入交易
                self._trade_no += 1
                qty = abs(order.executed.size)
                price = order.executed.price
                self.trade_records.append({
                    'trade_no': self._trade_no,
                    'direction': 'buy',
                    'trade_date': self.buy_date.strftime('%Y-%m-%d'),
                    'price': round(price, 2),
                    'quantity': qty,
                    'amount': round(price * qty, 2),
                    'commission': round(abs(order.executed.comm), 2),
                    'stamp_tax': 0,
                    'total_cost': round(abs(order.executed.comm), 2),
                    'pnl': None,
                    'pnl_pct': None,
                    'holding_days': None,
                    'signal_reason': self._pending_signal_reason or '',
                })
            elif order.issell():
                # 记录卖出交易（补充 quantity，因为 notify_trade 的 trade.size 可能为 0）
                qty = abs(order.executed.size)
                price = order.executed.price
                sell_date = self.data.datetime.date(0)
                holding_days = (sell_date - self.buy_date).days if self.buy_date else 0
                # 计算盈亏
                cost = self.buy_price * qty if self.buy_price else 0
                revenue = price * qty
                pnl = revenue - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0

                self._trade_no += 1
                self.trade_records.append({
                    'trade_no': self._trade_no,
                    'direction': 'sell',
                    'trade_date': sell_date.strftime('%Y-%m-%d'),
                    'price': round(price, 2),
                    'quantity': qty,
                    'amount': round(revenue, 2),
                    'commission': round(abs(order.executed.comm), 2),
                    'stamp_tax': 0,
                    'total_cost': round(abs(order.executed.comm), 2),
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                    'holding_days': holding_days,
                    'signal_reason': self._pending_signal_reason or '',
                })
            self.order = None
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.order = None

    def notify_trade(self, trade):
        # 交易记录已在 notify_order 中处理，此处仅更新内部状态
        if trade.isclosed:
            self.buy_price = None
            self.buy_date = None
            self.highest_since_buy = 0
            self._pending_signal_reason = None

    def next(self):
        """默认 next：检查止损止盈，然后调用子类逻辑"""
        # 更新最高价
        if self.position and self.data.close[0] > self.highest_since_buy:
            self.highest_since_buy = self.data.close[0]

        # 检查止损止盈
        self._check_stop_loss()
        self._check_take_profit()

        # 如果没有未完成订单，调用子类逻辑
        if not self.order:
            self.on_bar()

    def on_bar(self):
        """子类应重写此方法而非 next()"""
        pass

    def get_trades(self) -> List[Dict[str, Any]]:
        """获取交易记录"""
        return self.trade_records.copy()

    def get_signal_log(self) -> List[Dict[str, Any]]:
        """获取 scan 模式下记录的逐 bar 信号（按时间顺序，仅命中 bar）"""
        return self._signal_log.copy()

    @classmethod
    def get_params_info(cls) -> Dict[str, Any]:
        """返回参数说明，供前端渲染表单。子类可覆盖。"""
        return {
            'stop_loss': {'label': '止损比例', 'type': 'float', 'default': 0, 'min': 0, 'max': 100, 'unit': '%'},
            'take_profit': {'label': '止盈比例', 'type': 'float', 'default': 0, 'min': 0, 'max': 1000, 'unit': '%'},
            'trailing_stop': {'label': '移动止损', 'type': 'float', 'default': 0, 'min': 0, 'max': 100, 'unit': '%'},
        }

    @classmethod
    def get_chart_indicators(cls, params: dict) -> list:
        """声明回测结果 K 线图要叠加的指标。

        返回 [{'type': 'MA', 'period': 5, 'panel': 'main'}, ...]，默认不叠加。
        子类用 params.get(name, 默认值) 取该次回测的真实参数。
        """
        return []

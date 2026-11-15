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


TIMING_CHOICES = ('next_open', 'same_close', 'next_close')

TIMING_LABELS = {
    'next_open': '次日开盘价',
    'same_close': '当日收盘价（尾盘）',
    'next_close': '次日收盘价',
}


def normalize_timing(value, default: str = 'next_open') -> str:
    """归一化成交口径；非法值一律回落到default（不猜、不抛，回测口径错=结论整体失真）"""
    v = str(value or '').strip().lower()
    return v if v in TIMING_CHOICES else default


class StockSolveStrategy(bt.Strategy):
    """Stock Radar 策略基类

    子类只需实现 next() 方法，基类提供：
    - notify_order / notify_trade 统一处理
    - A股 T+1 规则（当天买入次日才能卖出）
    - 最小交易单位 100 股
    - 止损止盈（固定/移动）
    - 仓位管理（全仓/固定比例/固定金额）
    - 买卖**各自独立**的成交口径（尾盘买入 + 次日开盘卖这类组合才表达得出来）
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
        # ---- 成交口径（买/卖各自独立）----
        # 口径决定订单落在哪根 bar 的哪个价上成交，这是「尾盘买入、次日卖出」这类
        # 需求能否被真实表达的根本（此前只有一个全局 entry_timing + broker 级
        # set_coc，导致买卖两端被迫同口径）。
        #   next_open  : 发出信号的下一根 bar 开盘成交（backtrader 默认，不开 coc）
        #   same_close : 发出信号的当根 bar 收盘成交（等价 cheat-on-close = 尾盘）
        #   next_close : 下一根 bar 收盘成交
        ('entry_timing', 'next_open'),  # 买入口径
        ('exit_timing', 'next_open'),   # 卖出口径
        # 持有期卖点：买入后持有 N 根 K 线无条件卖出（0 = 不启用）。
        # 「尾盘买入、次日卖出」= 1。由基类统一实现，JSON DSL 的 hold_bars 落到这里。
        ('hold_bars', 0),
    )

    def __init__(self):
        self.order = None
        # 「尾盘买、次日开盘卖」的预挂卖单：必须在买入同一根 bar 就进入待撮合
        # 队列，才能在下一根 bar 开盘成交（详见 _submit_pre_exit_sell 的注释）。
        self._pre_exit_order = None
        self.buy_price = None
        self.buy_date = None
        self.highest_since_buy = 0
        self.trade_records: List[Dict[str, Any]] = []
        self._trade_no = 0
        # P0 §4.6 归因：「触发 N 次买卖信号」口径 = 已提交订单数（含后来被撮合拦截的）
        self._buy_attempts = 0
        self._sell_attempts = 0
        self._pending_signal_reason = None
        # scan 模式下逐 bar 记录买卖条件命中（无状态评估）
        self._signal_log: List[Dict[str, Any]] = []
        # 持仓已跨越的 bar 数（买入成交那根 bar 记0，其后每根 +1）。
        # 供「持有 N 根 K 线后无条件卖出」用（JSON DSL 的 hold_bars）。
        self.bars_since_buy = 0
        # 被 broker 拒单（Margin/Rejected）的订单数。**必须可观测**：拒单会让回测
        # 静默跑出「0 笔交易」而不报错，看着像策略没信号，实际是钱不够被拒。
        self.rejected_orders = 0
        self._last_reject: Optional[Dict[str, Any]] = None
        # 持有期倒计时：hold_bars - bars_since_buy。<=0 时该发卖单了。
        # 独立于 bars_since_buy，是因为 bars_since_buy 在成交那根才归零（见notify_order），
        # 用它当倒计时基准会在「买入当根就挂卖单」的场景下算错一根bar。
        self._hold_left = 0

    # -----------------------------------------------------------
    # 成交口径
    # -----------------------------------------------------------

    def _cost_basis_price(self) -> float:
        """按买入口径返回「预计成交价」，用于估算可买股数。

        - same_close : 本根收盘价（与实际成交一致）
        - next_open  : 下一根开盘价不可知，取 max(本根 close, 本根 open) 作为保守上界，
                       宁可少买一点，也不能因为跳空高开算多而被 Margin 拒单
        - next_close : 同 next_open（收盘价同样不可知，用本根上界）
        """
        close = float(self.data.close[0])
        open_ = float(self.data.open[0])
        if normalize_timing(self.p.entry_timing) == 'same_close':
            return close
        return max(close, open_)

    def _apply_timing(self, timing: str) -> dict:
        """按口径返回下单 kwargs；并给 broker 一个「默认 coc」提示。

        ⚠️ ``set_coc`` 是 broker 级全局开关。真正决定成交价的是**订单自己的**
        ``order.info['coc']``：``AStockBroker._try_exec`` 会在执行每张单前按这个
        标记临时切 ``self.p.coc``，执行完再恢复。所以买卖可以在同一根 bar 上
        分别用 ``same_close`` / ``next_open``，不再需要「卖单晚一根」的妥协。

        - next_open  : 不开 coc，Market 单 → 下一根 bar 开盘成交
        - same_close : 开 coc，Market 单 → 当根 bar 收盘成交（= 尾盘）
        - next_close : 不开 coc，Order.Close 单 → 下一根 bar 收盘成交
        """
        t = normalize_timing(timing)
        if t == 'same_close':
            try:
                self.broker.set_coc(True)
            except Exception:
                pass
            return {}
        try:
            self.broker.set_coc(False)
        except Exception:
            pass
        if t == 'next_close':
            return {'exectype': bt.Order.Close}
        return {}

    # -----------------------------------------------------------
    # 交易执行辅助方法
    # -----------------------------------------------------------

    def _has_pending(self) -> bool:
        """是否有未完成订单（买单 / 预挂卖单都算）。"""
        return bool(self.order or self._pre_exit_order)

    def _should_pre_submit_exit(self) -> bool:
        """是否需要「T 日买、T+1 开盘/收盘卖」的预挂卖单。

        只覆盖 hold_bars=1 这个最常见的隔夜形态；其他 N 仍走正常的 hold_bars
        倒计时（T+N 开盘/收盘卖）。
        """
        if normalize_timing(self.p.entry_timing) != 'same_close':
            return False
        # next_open / next_close 都在 T+1 成交；same_close 卖点已经在 T+1 收盘，
        # 不需要预挂（正常倒计时路径会走到）。
        if normalize_timing(self.p.exit_timing) not in ('next_open', 'next_close'):
            return False
        try:
            return int(self.p.hold_bars or 0) == 1
        except (TypeError, ValueError):
            return False

    def _tag_order(self, order, reason: str, timing: str) -> None:
        """给订单打上 coc 与原因快照。

        ``AStockBroker._try_exec`` 会按 ``order.info['coc']`` 在**执行每张单前**
        临时切 broker 的全局 coc —— 这是买卖同 bar 不同口径能成立的关键。
        """
        if order is None:
            return
        order.info['coc'] = (normalize_timing(timing) == 'same_close')
        order.info['bt_reason'] = reason or ''
        for _k, _v in self._order_rule_info().items():
            order.info[_k] = _v

    def _submit_pre_exit_sell(self, size: int, reason: str = '') -> None:
        """在买入同一根 bar 预挂卖出单（T+1 开盘成交）。

        为什么必须同 bar 预挂：``next()`` 在 bar 收盘后调用，此时提交的 Market 单
        只能等**再下一根** bar 开盘成交。等 ``notify_order`` 里再补挂，已经是 T+1
        的 broker 周期，卖单只能落到 T+2 开盘（2026-10-20 实测，不是理论推演）。
        同 bar 预挂 + per-order coc 才能做到「T 收盘买、T+1 开盘卖」。

        ⚠️ 卖单在提交时还没有持仓，broker 会在**执行时**校验；只要买单先于卖单
        进入队列（本方法在 buy 之后调用），T+1 先成交买单、再成交卖单。
        """
        if size <= 0:
            return
        merged = dict(self._apply_timing(self.p.exit_timing))
        self._pre_exit_order = self.sell(size=size, **merged)
        self._sell_attempts += 1
        self._tag_order(self._pre_exit_order,
                        reason or '持有 1 根后卖出（预挂）', self.p.exit_timing)
        # 标记「预挂卖单」：broker 在成交前要确认买单真的成交了，否则会变成裸卖空。
        self._pre_exit_order.info['bt_pre_exit'] = True

    def buy_with_reason(self, reason: str = '', **kwargs):
        """买入（带信号原因），成交价口径取 ``entry_timing``"""
        if self._has_pending():
            return  # 有未完成订单，跳过
        self._pending_signal_reason = reason
        size = self._calc_buy_size()
        if size > 0:
            merged = dict(self._apply_timing(self.p.entry_timing), **kwargs)
            self.order = self.buy(size=size, **merged)
            self._buy_attempts += 1
            self._tag_order(self.order, reason, self.p.entry_timing)
            # 持有期倒计时在**下单时**起算，而不是成交时。
            try:
                hb = int(self.p.hold_bars or 0)
            except (TypeError, ValueError):
                hb = 0
            # 倒计时口径随买卖口径变化：
            # - same_close 买入 + next_open/next_close 卖出：买在 T 收盘，卖单要在
            #   T+N 的开盘/收盘成交，必须提前一根发出（T+N-1），倒计时 N-1；
            #   N=1 由 _submit_pre_exit_sell 在 T 日预挂。
            # - 其余组合（尤其 entry=next_open）沿用 N：买入本身已经晚一根，
            #   再减一就会把「持有 2 根」跑成「持有 1 根」。
            if (hb > 0
                    and normalize_timing(self.p.entry_timing) == 'same_close'
                    and normalize_timing(self.p.exit_timing) in ('next_open', 'next_close')):
                hb = max(hb - 1, 0)
            self._hold_left = hb
            # 尾盘买、次日开盘卖：卖单必须在同一根 bar 进入待撮合队列。
            if self._should_pre_submit_exit():
                self._submit_pre_exit_sell(size, reason='持有 1 根后卖出')

    def sell_with_reason(self, reason: str = '', **kwargs):
        """卖出（带信号原因），成交价口径取 ``exit_timing``"""
        if self._has_pending():
            return
        if not self.position:
            return
        self._pending_signal_reason = reason
        merged = dict(self._apply_timing(self.p.exit_timing), **kwargs)
        self.order = self.sell(size=self.position.size, **merged)
        self._sell_attempts += 1
        self._tag_order(self.order, reason, self.p.exit_timing)

    def hold_bars_reached(self, n: int = 0) -> bool:
        """持有期倒计时是否已到 0（n<=0 时用 ``params.hold_bars``；<=0 视为不启用）。

        以「下单时刻」为基准逐根递减，而不是拿成交后的 bars_since_buy 比大小——
        后者会让「持有 N根」实际变成 N+1 根（成交要等到下一根 bar 才通知到策略）。
        """
        try:
            n = int(n if n else (self.p.hold_bars or 0))
        except (TypeError, ValueError):
            return False
        if n <= 0:
            return False
        return self._hold_left <= 0

    def _calc_buy_size(self) -> int:
        """计算买入数量（考虑仓位和最小交易单位）

        A股规则：最小交易单位为 100 股（1 手）。
        当资金不足以买 1 手时，尝试买 1 手（允许透支，由broker 拒绝）。

        ⚠️ 计价基准必须与实际成交价一致：next_open 口径成交在**下一根 bar 开盘价**，
        若仍按本根 close 计价，遇到跳空高开就会算多→ 订单被 broker 以 Margin 拒绝，
        回测结果是「0 笔交易」且**不报错**（最隐蔽的一类静默失效）。
        """
        cash = self.broker.getcash()
        price = self._cost_basis_price()
        if price <= 0:
            return 0

        if self.p.position_mode == 'full':
            #留 1% 给佣金 + 成交价上冲，避免满仓被 Margin 拒单
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
        """检查 T+1 规则：当天买入不能当天卖

        用 bar 计数（``bars_since_buy >= 1``）而不是日期比较：
        same_close 口径下 buy_date 需要回退一根 bar（成交日在上一根），按日期比较会
        在买入当根就误判为「已隔日」，T+1 直接失效。

        注意这里**不再**拿 ``buy_date is None`` 当放行条件：bar 计数已经完整表达了
        「是否已隔一根 K 线」，再叠一个日期存在性判断只会让「没记录买入日」变成
        T+1 形同虚设（测试里就是这么暴露出来的）。
        """
        if not self.p.t_plus_1:
            return True
        return self.bars_since_buy >= 1

    def _order_rule_info(self) -> dict:
        """下单时快照创建 bar 的规则上下文（P0 §4.4）：coc 口径下 broker 回看不到这根 bar。"""
        pc_line = getattr(self.data, 'pre_close', None)
        try:
            pc = float(pc_line[0]) if pc_line is not None else float('nan')
        except (TypeError, ValueError, IndexError):
            pc = float('nan')
        try:
            vol = float(self.data.volume[0] or 0)
        except (TypeError, ValueError):
            vol = float('nan')
        return {'bt_pre_close': pc, 'bt_volume': vol}

    def _calc_slippage_cost(self, order) -> float:
        """逐笔滑点成本 = |实际成交价 − 未滑点理想价| × 成交量（P0 §4.7）。

        理想价由 ``AStockBroker`` 在 ``_try_exec`` 贴到 ``order.info['bt_ideal_price']``；
        未贴（旧路径 / 未成交）时返回 0，向后兼容。
        """
        ideal = order.info.get('bt_ideal_price', None)
        if ideal is None:
            return 0.0
        try:
            return round(abs(order.executed.price - float(ideal)) * abs(order.executed.size), 2)
        except (TypeError, ValueError):
            return 0.0

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
        # 订单级原因：同一根 bar 可能同时有买单和预挂卖单，不能共用一个
        # _pending_signal_reason（否则买单会记成卖单的原因）。
        _reason = str(order.info.get('bt_reason') or self._pending_signal_reason or '')
        # ⚠️ same_close（cheat-on-close）口径下订单在本根 bar 收盘价成交，但撮合通知
        # 到达时 data.datetime.date(0) 已经推进到下一根 → 交易记录日期比真实成交日晚
        # 一天。所以 same_close 一律取上一根的日期作为成交日。
        # 这不是小数点问题：K 线图上的买卖点与 trade_records 的日期对不上。
        _fill_shift = -1 if normalize_timing(self.p.entry_timing) == 'same_close' else 0
        if order.status == order.Completed:
            if order.isbuy():
                self.buy_price = order.executed.price
                self.buy_date = self.data.datetime.date(_fill_shift)
                self.highest_since_buy = order.executed.price
                # 买入成交那根 bar 记0，其后每根 bar +1（hold_bars 计数基准）
                self.bars_since_buy = 0
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
                    'slippage_cost': self._calc_slippage_cost(order),
                    'total_cost': round(abs(order.executed.comm), 2),
                    'pnl': None,
                    'pnl_pct': None,
                    'holding_days': None,
                    'signal_reason': _reason,
                })
            elif order.issell():
                # 记录卖出交易（补充 quantity，因为 notify_trade 的 trade.size 可能为 0）
                qty = abs(order.executed.size)
                price = order.executed.price
                sell_shift = -1 if normalize_timing(self.p.exit_timing) == 'same_close' else 0
                sell_date = self.data.datetime.date(sell_shift)
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
                    'slippage_cost': self._calc_slippage_cost(order),
                    'total_cost': round(abs(order.executed.comm), 2),
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                    'holding_days': holding_days,
                    'signal_reason': _reason,
                })
            # ⚠️ backtrader 在 notify 时给的是订单的副本（ref 相同、对象不同），
            # 不能用 `order is self.order` 判断；否则 self.order 永远清不掉，
            # 后续 on_bar 全被挡住（实测：只买不卖）。
            _same_as_main = (self.order is not None
                             and getattr(order, 'ref', None) == getattr(self.order, 'ref', None))
            _same_as_pre = (self._pre_exit_order is not None
                            and getattr(order, 'ref', None) == getattr(self._pre_exit_order, 'ref', None))
            if _same_as_main:
                self.order = None
            if _same_as_pre:
                self._pre_exit_order = None
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            if order.status in (order.Margin, order.Rejected):
                self.rejected_orders += 1
                self._last_reject = {
                    'status': int(order.status),
                    'size': abs(order.size),
                    'cash': float(self.broker.getcash()),
                    'date': self.data.datetime.date(0).strftime('%Y-%m-%d'),
                    'entry_timing': normalize_timing(self.p.entry_timing),
                }
            _same_as_main = (self.order is not None
                             and getattr(order, 'ref', None) == getattr(self.order, 'ref', None))
            _same_as_pre = (self._pre_exit_order is not None
                            and getattr(order, 'ref', None) == getattr(self._pre_exit_order, 'ref', None))
            if _same_as_main:
                self.order = None
                # 买单被拒/撤销 → 同 bar 预挂的卖单必须一起撤销；
                # 否则它会变成一张没有持仓支撑的裸卖单。
                if self._pre_exit_order is not None:
                    try:
                        self.cancel(self._pre_exit_order)
                    except Exception:
                        pass
                    self._pre_exit_order = None
            if _same_as_pre:
                self._pre_exit_order = None

    def notify_trade(self, trade):
        # 交易记录已在 notify_order 中处理，此处仅更新内部状态
        if trade.isclosed:
            self.buy_price = None
            self.buy_date = None
            self.highest_since_buy = 0
            self.bars_since_buy = 0
            self._hold_left = 0
            self._pending_signal_reason = None
            self._pre_exit_order = None

    def next(self):
        """默认 next：检查止损止盈，然后调用子类逻辑"""
        # 持仓 bar 计数（hold_bars 用）：买入成交那根 bar 已由 notify_order 置 0，
        # 此后每根 bar +1。空仓时归零，避免残留值影响下一次持仓。
        if self.position:
            self.bars_since_buy += 1
        else:
            self.bars_since_buy = 0

        # 持有期倒计时逐根递减（有持仓才走）。到 0 后由 on_bar / 止损逻辑去发卖单。
        if self.position and self._hold_left > 0:
            self._hold_left -= 1

        # 更新最高价
        if self.position and self.data.close[0] > self.highest_since_buy:
            self.highest_since_buy = self.data.close[0]

        # 检查止损止盈
        self._check_stop_loss()
        self._check_take_profit()

        # 如果没有未完成订单（含预挂卖单），调用子类逻辑
        if not self._has_pending():
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

    def get_order_attempts(self) -> Dict[str, int]:
        """下单尝试计数（P0 §4.6 归因用）：「触发 N 次信号」口径 = 已提交订单数。"""
        return {'buy': self._buy_attempts, 'sell': self._sell_attempts}

    def get_reject_info(self) -> Dict[str, Any]:
        """被broker 拒单的诊断信息。

        0 笔交易但 rejected_orders > 0 = 钱不够被拒，不是策略没信号 ——
        两者的处置完全不同（前者调仓位口径，后者改买卖条件），必须能分开看。
        """
        return {'rejected_orders': self.rejected_orders, 'last': self._last_reject}

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

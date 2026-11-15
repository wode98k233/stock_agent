"""A 股交易规则

实现 A 股特有的交易规则：
- 佣金：按比例收取，单笔最低 5 元
- 印花税：卖出时收取 0.05%
- 过户费：沪市 0.001%
- T+1：当天买入次日才能卖出（在策略基类中实现）
- 最小交易单位：100 股（在策略基类中实现）
- 撮合保真（P0 设计 §4.4）：涨跌停 / 停牌不可成交（``AStockBroker``，Broker 层拦截）
- 滑点接线（P0 设计 §4.5）：``setup_broker`` 接 ``set_slippage_perc`` + 逐笔记账
"""
import logging
import math

import backtrader as bt

from backtest import a_share_rules

logger = logging.getLogger(__name__)


class AStockCommission(bt.CommInfoBase):
    """A 股佣金方案

    买入：佣金（按比例，最低 min_commission）
    卖出：佣金 + 印花税 + 过户费

    口径说明（选型清单 P0-#6）：佣金「万三」按**已含经手费（0.00341%）与证管费
    （0.002%）**的市场惯例近似——两者合计 ≈ 万 0.54，券商佣金通常已包含，单独拆分
    徒增复杂度但误差可忽略；报告侧在 warnings 中明示该口径（对用户透明）。
    """

    params = (
        ('commission', 0.0003),       # 佣金率（万三）
        ('min_commission', 5.0),      # 最低佣金
        ('stamp_tax', 0.0005),        # 印花税（万五，仅卖出）
        ('transfer_fee', 0.00001),    # 过户费（十万分之一）
        ('stocklike', True),
        ('commtype', bt.CommInfoBase.COMM_PERC),
        # 必须开：我们的费率是小数比率（0.0003 = 万三），而 backtrader 在
        # COMM_PERC 下默认把 commission 当"百分数"再除以 100
        # （CommInfoBase.__init__: if commtype == PERC and not percabs: commission /= 100）。
        # 不开这行，万三会变成十万分之三，每笔佣金都落到最低 5 元——
        # 2026-11-06 实测：98.8 万成交额只记了 14.88 元佣金（5 元最低 + 9.88 过户费）。
        ('percabs', True),
    )

    def _getcommission(self, size, price, pseudoexec):
        """计算佣金（含印花税和过户费）"""
        turnover = abs(size) * price

        # 基础佣金
        commission = turnover * self.p.commission
        commission = max(commission, self.p.min_commission)

        # 卖出时加印花税
        if size < 0:
            commission += turnover * self.p.stamp_tax

        # 过户费
        commission += turnover * self.p.transfer_fee

        return commission


class AStockBroker(bt.brokers.BackBroker):
    """A 股撮合经纪商：涨跌停 / 停牌不可成交 + 滑点记账 + 拒单归因（P0 设计 §4.4）。

    拦截点必须在 Broker 层（设计 §3）：cerebro 每根 bar 先 ``_brokernotify()``
    再 ``strategy._next()``，策略层闸门一定晚一步。

    订单语义（设计 §3.3）：被拒订单保持 alive，但**次根即被 ``_expire_blocked``
    撤销**（当日有效、零重试）——次日想买要由策略重新挂单（与实盘一致）。
    """

    def __init__(self, *args, rule_ctx: dict = None, **kwargs):
        self.rule_ctx = rule_ctx or {}          # {code, limit_pct, mode}
        self.blocked = {'limit_up': 0, 'limit_down': 0, 'suspended': 0, 'expired': 0}
        self.blocked_detail = []                # [{date, side, reason, size}]
        self.slippage_cost = 0.0
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # 取价与判定
    # ------------------------------------------------------------------

    def _exec_price_ctx(self, order):
        """返回 ``(exec_price, pre_close, volume, same_close)``。

        复现 ``bbroker.py:1043-1055`` 的取价（优先 ``tick_*`` 覆写，否则 data 当前值）。
        **仅需覆盖 Market / Close 两种 exectype**（项目现用口径）；将来若引入
        Limit / Stop 单，判定口径需按 bbroker ``:1057-1075`` 的分派同步扩展。

        pre_close 来源按口径分派（设计 §4.4 表）：

        - next_open / next_close：``order.data.pre_close[0]``（当前 bar，feed line）
        - same_close（coc）：``order.info['bt_pre_close']``（策略下单时快照的**创建 bar**
          值——撮合发生在次根，broker 回看不到创建 bar）
        """
        d = order.data
        popen = getattr(d, 'tick_open', None)
        if popen is None:
            popen = d.open[0]
        pclose_ = getattr(d, 'tick_close', None)
        if pclose_ is None:
            pclose_ = d.close[0]

        if order.exectype == bt.Order.Market:
            if self.p.coc and order.info.get('coc', True):
                # same_close：成交在**创建当根**收盘 → 判定用创建 bar 的 pre_close
                exec_price = float(order.created.pclose)
                same_close = True
            else:
                exec_price = float(popen)
                same_close = False
        elif order.exectype == bt.Order.Close:
            exec_price = float(pclose_)
            same_close = False
        else:
            # 其他 exectype（Limit/Stop）：popen 兜底（项目未使用，属防御）
            exec_price = float(popen)
            same_close = False

        if same_close:
            pc_raw = order.info.get('bt_pre_close', None)
        else:
            pc_line = getattr(d, 'pre_close', None)
            try:
                pc_raw = pc_line[0] if pc_line is not None else None
            except (TypeError, IndexError):
                pc_raw = None
        try:
            pre_close = float(pc_raw) if pc_raw is not None else float('nan')
        except (TypeError, ValueError):
            pre_close = float('nan')

        try:
            volume = float(d.volume[0])
        except (TypeError, ValueError):
            volume = float('nan')

        return exec_price, pre_close, volume, same_close

    def _check(self, order, exec_price, pre_close, volume, same_close) -> str:
        """可成交性判定（顺序即优先级，设计 §4.4 判定链）。

        返回 ``''``（可成交）| ``'suspended'`` | ``'limit_up'`` | ``'limit_down'``。
        """
        if a_share_rules.is_suspended(volume):
            return 'suspended'
        pct = self.rule_ctx.get('limit_pct')
        if pct is None:
            return ''   # 无涨跌停约束（ETF/指数/板块/未知前缀），fail-open
        if pre_close is None or math.isnan(pre_close) or pre_close <= 0:
            return ''   # 昨收不可用，fail-open
        side = 'buy' if order.isbuy() else 'sell'
        return a_share_rules.is_fill_blocked(side, exec_price, pre_close, pct)

    # ------------------------------------------------------------------
    # 拦截与记账
    # ------------------------------------------------------------------

    def _record_block(self, order, reason: str) -> None:
        """记录拒单（计数 + 明细 + 被拒时刻标记）。"""
        self.blocked[reason] = self.blocked.get(reason, 0) + 1
        try:
            date_str = order.data.datetime.datetime(0).strftime('%Y-%m-%d')
        except Exception:  # noqa: BLE001 - 记录型逻辑，不因格式化失败中断撮合
            date_str = ''
        self.blocked_detail.append({
            'date': date_str,
            'side': 'buy' if order.isbuy() else 'sell',
            'reason': reason,
            'size': abs(order.created.size),
        })
        # 被拒时刻：_expire_blocked 以此判定「次根作废」（当日有效语义）
        order.info['bt_blocked_dt'] = float(order.data.datetime[0])

    def _try_exec(self, order):
        # 按订单切换 coc：同一根 bar 上「买单 same_close + 卖单 next_open」才能各按各的
        # 价成交。以前只在策略下单前切 broker 全局 coc，执行时两张单读到的是同一个
        # 值，于是买卖被迫同口径（「尾盘买、次日开盘卖」因此晚一根 bar）。
        _old_coc = self.p.coc
        self.p.coc = bool(order.info.get('coc', _old_coc))
        try:
            # 预挂卖单只有在买单真的成交、持仓足够时才允许执行；否则会在涨停买不进
            # 等场景下变成裸卖空。买单先入队，正常情况下这里 position 已经够了。
            if order.info.get('bt_pre_exit'):
                pos = self.getposition(order.data).size
                if pos < abs(order.size):
                    self._record_block(order, 'pre_exit_unfilled')
                    return
            mode = self.rule_ctx.get('mode', 'enforce')
            exec_price, pre_close, volume, same_close = self._exec_price_ctx(order)
            if mode != 'off':
                reason = self._check(order, exec_price, pre_close, volume, same_close)
                if reason:
                    self._record_block(order, reason)   # warn 档只记录、不拦
                    if mode == 'enforce':
                        return                          # 不执行（次根作废，零重试，§3.3）
            # 滑点记账（§4.7-1）：broker 只把「未滑点的理想价」贴到订单上，
            # 逐笔成本由策略在 notify_order 里算（列 backtest_trades.slippage_cost）。
            # off 档同样贴价——滑点与涨跌停拦截是两件事。
            order.info['bt_ideal_price'] = exec_price
            super()._try_exec(order)
            if order.status == order.Completed:
                self.slippage_cost += abs(order.executed.price - exec_price) * abs(order.executed.size)
        finally:
            self.p.coc = _old_coc

    def _expire_blocked(self) -> None:
        """委托当日有效：被拒订单在**次根**撤销（零重试，设计 §3.3）。

        ⚠️ 必须用 ``list(self.pending)`` 快照后遍历：``cancel()`` 的第一件事就是
        ``self.pending.remove(order)``（``bbroker.py:391-395``），边遍历 deque 边撤
        会直接 ``RuntimeError: deque mutated during iteration``。
        """
        for order in list(self.pending):
            blocked_dt = order.info.get('bt_blocked_dt', None)
            if blocked_dt is None:
                continue
            try:
                cur_dt = float(order.data.datetime[0])
            except Exception:  # noqa: BLE001
                continue
            if blocked_dt < cur_dt:
                if self.cancel(order):      # 不在 pending 时返回 False 且无副作用
                    self.blocked['expired'] = self.blocked.get('expired', 0) + 1

    def next(self):
        self._expire_blocked()              # 先撤被拒单（当日有效），再处理 pending
        super().next()


def _lookup_stock_name(code: str) -> str:
    """本地查股票简称（ST 判定用）——读 stock_info 表，不走实时接口。"""
    try:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            row = conn.execute(
                'SELECT name FROM stock_info WHERE code = ? LIMIT 1', (code,)).fetchone()
        return (str(row[0]) if row and row[0] else '')
    except Exception:  # noqa: BLE001 - 查名失败退化为非 ST（fail-open）
        return ''


def setup_broker(cerebro: bt.Cerebro, config: dict) -> None:
    """配置 Cerebro 的 A 股经纪商参数（P0 设计 §4.5）

    ⚠️ 换装 ``AStockBroker`` 必须**在 setcash / addcommissioninfo 之前**，
    否则资金与佣金方案会落在被丢弃的默认 ``BackBroker`` 上。

    Args:
        cerebro: backtrader Cerebro 实例
        config: 回测配置字典（code / asset_type / slippage / price_limit_mode ...）
    """
    asset_type = config.get('asset_type', 'stock')
    code = str(config.get('code') or '')

    # 非 stock 一律不启用涨跌停（ETF 的 10%/20% 无法用代码可靠区分，设计 §4.1）。
    # ETF 走 a_share_rules.etf_limit_pct 这个显式入口（当前恒 None = 不拦），
    # 由 engine 在 warnings / health_detail 里声明「未模拟」，不伪装成已拦。
    pct = None
    if asset_type == 'stock' and code:
        pct = a_share_rules.limit_pct(code, _lookup_stock_name(code))
    elif asset_type == 'etf':
        pct = a_share_rules.etf_limit_pct(code)

    # 口径模式：单次请求 > 全局配置（PRICE_LIMIT_MODE，默认 enforce）
    try:
        from config import Config
        _cfg_mode = getattr(Config, 'PRICE_LIMIT_MODE', 'enforce')
    except Exception:  # noqa: BLE001
        _cfg_mode = 'enforce'
    mode = a_share_rules.resolve_mode(config.get('price_limit_mode'), _cfg_mode)

    broker = AStockBroker(rule_ctx={'code': code, 'limit_pct': pct, 'mode': mode})
    cerebro.setbroker(broker)

    # 设置初始资金
    broker.setcash(config.get('initial_cash', 100000))

    # 滑点接线（设计 §4.5）：slippage > 0 才调用——方法默认 slip_open=True
    # （⚠️ 不要手写 slip_open 的 params 覆盖成 False，否则开盘价不滑、等于白配）。
    try:
        slippage = float(config.get('slippage', 0.001) or 0)
    except (TypeError, ValueError):
        slippage = 0.0
    if slippage > 0:
        broker.set_slippage_perc(slippage)

    # 根据资产类型调整佣金
    stamp_tax = config.get('stamp_tax', 0.0005)
    if asset_type == 'etf':
        # ETF 无印花税
        stamp_tax = 0

    # 设置 A 股佣金方案
    commission_info = AStockCommission(
        commission=config.get('commission', 0.0003),
        min_commission=config.get('min_commission', 5.0),
        stamp_tax=stamp_tax,
        transfer_fee=config.get('transfer_fee', 0.00001),
    )
    broker.addcommissioninfo(commission_info)

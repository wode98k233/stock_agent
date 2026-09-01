"""A 股交易规则

实现 A 股特有的交易规则：
- 佣金：按比例收取，单笔最低 5 元
- 印花税：卖出时收取 0.05%
- 过户费：沪市 0.001%
- T+1：当天买入次日才能卖出（在策略基类中实现）
- 最小交易单位：100 股（在策略基类中实现）
"""
import backtrader as bt


class AStockCommission(bt.CommInfoBase):
    """A 股佣金方案

    买入：佣金（按比例，最低 min_commission）
    卖出：佣金 + 印花税 + 过户费
    """

    params = (
        ('commission', 0.0003),       # 佣金率（万三）
        ('min_commission', 5.0),      # 最低佣金
        ('stamp_tax', 0.0005),        # 印花税（万五，仅卖出）
        ('transfer_fee', 0.00001),    # 过户费（十万分之一）
        ('stocklike', True),
        ('commtype', bt.CommInfoBase.COMM_PERC),
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


def setup_broker(cerebro: bt.Cerebro, config: dict) -> None:
    """配置 Cerebro 的 A 股经纪商参数

    Args:
        cerebro: backtrader Cerebro 实例
        config: 回测配置字典
    """
    # 设置初始资金
    cerebro.broker.setcash(config.get('initial_cash', 100000))

    # 根据资产类型调整佣金
    asset_type = config.get('asset_type', 'stock')
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
    cerebro.broker.addcommissioninfo(commission_info)

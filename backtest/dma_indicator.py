"""DMA (Difference of Moving Average) 平行线差指标

公式（与通达信/同花顺一致）:
    DIF = MA(收盘价, short) - MA(收盘价, long)
    AMA = MA(DIF, smooth)

设计要点:
- DIF 为长短两条均线的差值，反映趋势方向：DIF>0 多头、DIF<0 空头。
- AMA 是 DIF 的平滑线，作为信号线：DIF>AMA 趋势向上、DIF<AMA 趋势向下。
- 常用于判断大趋势：DIF 在 0 轴下方但上穿 AMA，往往是底部反转的早期信号。

默认参数: short=10, long=50, smooth=6（经典默认）。
"""
import backtrader as bt


class DMA(bt.Indicator):
    """平行线差 DIF + 其平滑线 AMA"""

    lines = ('dif', 'ama')
    params = (
        ('short', 10),     # 短期均线周期
        ('long', 50),      # 长期均线周期
        ('smooth', 6),     # AMA 平滑周期
    )

    def __init__(self):
        ma_short = bt.indicators.SMA(self.data, period=self.p.short)
        ma_long = bt.indicators.SMA(self.data, period=self.p.long)
        self.lines.dif = ma_short - ma_long
        self.lines.ama = bt.indicators.SMA(self.lines.dif, period=self.p.smooth)

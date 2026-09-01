"""区间震荡线 (Detrended Price Oscillator, DPO) 自定义 Backtrader 指标

公式（与通达信/同花顺一致）:
    DPO   = 收盘价 - REF(MA(收盘价, period), period // 2 + 1)
    MADPO = MA(DPO, smooth)

设计要点:
- 通过把 N 日简单移动平均「平移」(period // 2 + 1) 期，把长期趋势拉直成 0 轴，
  仅保留价格相对历史均值的短期偏离，从而消除趋势对震荡信号的干扰。
- DPO > 0 表示处于多头市场，DPO < 0 表示处于空头市场。
- 在 0 轴上方设超买线、下方设超卖线，触及即形成短期高/低点（阈值随个股不同需自调）。
- DPO 上穿 MADPO（金叉）买入、下穿（死叉）卖出；该信号通常早于 MACD / KDJ。

默认参数: period=20, smooth=6（经典默认）。shift 可显式覆盖平移量。
"""
import backtrader as bt


class DPO(bt.Indicator):
    """区间震荡线 + 其平滑线 MADPO"""

    lines = ('dpo', 'madpo')
    params = (
        ('period', 20),     # DPO 计算周期（简单移动平均窗口）
        ('smooth', 6),      # MADPO 平滑周期
        ('shift', None),    # 平移量；None 时自动取 period // 2 + 1
    )

    def __init__(self):
        shift = self.p.shift if self.p.shift is not None else (self.p.period // 2 + 1)

        sma = bt.indicators.SMA(self.data, period=self.p.period)
        # 当前收盘价 与 shift 期前的 N 日简单移动平均之差（去趋势化）
        self.lines.dpo = self.data - sma(-shift)
        # MADPO：DPO 的平滑均线，作为信号线
        self.lines.madpo = bt.indicators.SMA(self.lines.dpo, period=self.p.smooth)

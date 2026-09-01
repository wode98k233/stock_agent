"""CYW 主力控盘指标（同花顺 / 指南针标准版本）

公式（同花顺主力控盘 CYW，系统标准实现）:
    VAR1 = CLOSE - LOW
    VAR2 = HIGH - LOW
    VAR3 = CLOSE - HIGH
    VAR4 = IF(HIGH > LOW, (VAR1/VAR2 + VAR3/VAR2) * VOL, 0)
         = IF(HIGH > LOW, (2*CLOSE - HIGH - LOW) / (HIGH - LOW) * VOL, 0)
    CYW  = SUM(VAR4, period) / 10000

语义:
- CYW > 0 表示存在主力控盘行为（红柱），数值越大控盘越强；
- CYW < 0 表示未发现主力控盘（蓝柱），0 轴为多空分界。
- 一字板 / 停牌（HIGH == LOW）时 VAR4 记 0，避免除零。

实现说明:
- 本指标需绑定完整 data feed（含 high/low/close/volume），
  因此在 json_compiler 中按 KDJ 同样的方式特化构造：不传 data line，
  由 Backtrader 自动绑定到策略的 data feed，从而可取 high/low/volume。
- 使用 bt.DivByZero 处理 HIGH==LOW 的除零，与同花顺 IF(HIGH>LOW,...,0) 一致。

默认参数: period=10（同花顺默认的 10 日求和窗口）。
"""
import backtrader as bt


class CYW(bt.Indicator):
    """主力控盘指标（同花顺版）"""

    lines = ('cyw',)
    params = (
        ('period', 10),
    )

    def __init__(self):
        close = self.data.close
        high = self.data.high
        low = self.data.low
        vol = self.data.volume

        # (2*CLOSE - HIGH - LOW) / (HIGH - LOW) * VOL
        # = (VAR1 + VAR3) / VAR2 * VOL = (VAR1/VAR2 + VAR3/VAR2) * VOL
        # 分母为 0（一字板/停牌）时按同花顺公式归 0
        ratio = bt.DivByZero(2 * close - high - low, high - low, zero=0.0)
        var4 = ratio * vol
        self.lines.cyw = bt.indicators.SumN(var4, period=self.p.period) / 10000.0

"""回测分析器

自定义 Backtrader Analyzer，提供扩展统计指标。
"""
import backtrader as bt


class NeutralBandAnalyzer(bt.Analyzer):
    """中性带宽分析器

    将收益在 ±neutral_band_pct% 内的交易视为 "中性"（不归入胜/负）。
    用于过滤小幅度波动的噪声，使胜率更有参考价值。

    参数:
        neutral_band_pct: 中性带宽百分比，默认 2.0（即 ±2% 内视为中性）

    输出（通过 get_analysis() 获取）:
        neutral_count: 中性交易数
        neutral_rate: 中性交易占比
        win_count: 排除中性后的胜数
        loss_count: 排除中性后的负数
        win_rate: 排除中性后的胜率
        effective_total: 排除中性后的有效交易总数
    """

    params = (
        ('neutral_band_pct', 2.0),
    )

    def __init__(self):
        self._pnl_trades = []

    def notify_trade(self, trade):
        """记录每笔已完成交易的收益率"""
        if trade.isclosed:
            # 避免除零：用 pnl 和 executed value 计算
            if trade.price > 0 and abs(trade.size) > 0:
                base = trade.price * abs(trade.size)
                pnl_pct = (trade.pnl / base) * 100 if base > 0 else 0
            else:
                pnl_pct = 0
            self._pnl_trades.append(pnl_pct)

    def stop(self):
        """回测结束时计算统计，写入 self.rets"""
        band = self.p.neutral_band_pct
        neutral = [t for t in self._pnl_trades if abs(t) <= band]
        wins = [t for t in self._pnl_trades if t > band]
        losses = [t for t in self._pnl_trades if t < -band]
        effective = len(wins) + len(losses)

        # Backtrader Analyzer 使用 self.rets 存储结果
        self.rets['neutral_count'] = len(neutral)
        self.rets['neutral_rate'] = len(neutral) / len(self._pnl_trades) if self._pnl_trades else 0
        self.rets['win_count'] = len(wins)
        self.rets['loss_count'] = len(losses)
        self.rets['win_rate'] = len(wins) / effective if effective > 0 else 0
        self.rets['effective_total'] = effective
        self.rets['neutral_band_pct'] = band

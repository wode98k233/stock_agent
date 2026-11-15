"""回测分析器

自定义 Backtrader Analyzer，提供扩展统计指标。
"""
import backtrader as bt


class NeutralBandAnalyzer(bt.Analyzer):
    """中性带宽分析器

    将收益在 ±neutral_band_pct% 内的交易视为 "中性"（不归入胜/负）。
    用于过滤小幅度波动的噪声，使胜率更有参考价值。

    参数:
        neutral_band_pct: 中性带宽百分比，默认 0.1（即 ±0.1% 内视为中性）

    ⚠️ 默认值 2026-10-18 由 2.0 改为 0.1（用户拍板）：口径是「赚了超过 0.1% 算赢、
    亏了 0.1% 算输」。旧默认 ±2% 对「尾盘买入次日卖出」这类 1 日持有策略会把
    73/74 笔判成中性，胜率根本算不出来。

    输出（通过 get_analysis() 获取）:
        neutral_count: 中性交易数
        neutral_rate: 中性交易占比
        win_count: 排除中性后的胜数
        loss_count: 排除中性后的负数
        win_rate: 排除中性后的胜率
        effective_total: 排除中性后的有效交易总数
    """

    params = (
        ('neutral_band_pct', 0.1),
    )

    def __init__(self):
        self._pnl_trades = []
        self._base = {}   # trade.ref -> 开仓时的持仓市值（平仓时 size 已归零，必须提前记）

    def notify_trade(self, trade):
        """记录每笔已完成交易的收益率。

        ⚠️ 必须在**持仓期间**记下 base（2026-10-18 修）：平仓那一刻 trade.size 已经是 0，
        旧代码用 trade.price * abs(trade.size) 作分母，守卫命中 else 分支 → pnl_pct 恒为 0
        → 所有交易都被判成「中性」→ 胜率永远算不出来（实测 74 笔里 73 笔中性，
        把带宽从 2% 收到 0.1% 也毫无变化，才暴露出来）。

        口径：用**净收益** trade.pnlcomm（含佣金/印花税/滑点）算每笔收益率——
        「赚了超过 0.1% 算赢」指的是扣完成本后还赚 0.1%，用毛利会把成本吃掉的部分算成赢。
        """
        ref = getattr(trade, 'ref', id(trade))
        if trade.isclosed:
            base = self._base.pop(ref, 0.0)
            pnl = trade.pnlcomm if trade.pnlcomm is not None else trade.pnl
            self._pnl_trades.append((pnl / base) * 100 if base > 0 else 0.0)
            return
        if trade.isopen:
            # 持仓期间记录（加仓取最大值，保证分母是实际动用的资金）
            base = abs(trade.price) * abs(trade.size)
            if base > self._base.get(ref, 0.0):
                self._base[ref] = base

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
        # ⚠️ 有效样本为 0 时必须给 None，不能给 0（2026-10-18 实测事故）：
        # 默认中性带 ±2%，而「尾盘买入次日卖出」这类 1 日持有策略的每笔收益几乎都在
        # ±2% 内 → effective=0 → 这里若返回 0，等于把「全部交易都算中性、无从判断」
        # 冒充成「胜率 0%」。实测 300 只沪深300成分股全部报 0%，其中还有总收益 +46%
        # 的标的，自相矛盾。口径同 sharpe：算不出来就是 None。
        self.rets['win_rate'] = len(wins) / effective if effective > 0 else None
        self.rets['effective_total'] = effective
        self.rets['neutral_band_pct'] = band

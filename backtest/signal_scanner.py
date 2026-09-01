"""无状态策略信号扫描器

逐 K 线独立评估策略买入/卖出条件树，不依赖持仓状态。
最新一根 bar 的布尔值即"今日是否买点"。

与回测的区别：
- 回测有状态（持仓/订单/T+1 影响），scan 无状态（每根 bar 独立评估）
- scan 只记录命中 bar，不实际下单
- scan 买点日期 ⊇ 回测实际买入日期（回测受持仓状态约束会跳过部分买点）

数据口径与 build_run_kline 一致：_load_stock_daily(auto_heal=False) + _apply_adjustment
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import backtrader as bt
import pandas as pd

from backtest.data_adapter import _load_stock_daily, _apply_adjustment, _normalize_columns
from backtest.strategies import get_strategy_class

logger = logging.getLogger(__name__)


class SignalScanner:
    """无状态策略信号扫描器

    用法：
        scanner = SignalScanner()
        result = scanner.scan(code='600519', strategy_id='dual_ma_crossover',
                              start_date='2025-01-01', end_date='2026-01-01')
        # result['latest_is_buy'] → 今日是否买点
    """

    def scan(self, code: str, strategy_id: str,
             start_date: Optional[str] = None,
             end_date: Optional[str] = None,
             params: Optional[Dict[str, Any]] = None,
             adjust_type: str = 'qfq',
             days: int = 120) -> Dict[str, Any]:
        """执行无状态信号扫描

        Args:
            code: 股票代码
            strategy_id: 策略 ID
            start_date: 开始日期（YYYY-MM-DD），缺省时按 days 回溯
            end_date: 结束日期，缺省时用今天
            params: 策略参数覆盖（如 {'fast_period': 10}）
            adjust_type: 复权类型 qfq / hfq / none
            days: start_date 缺省时的回溯天数

        Returns:
            {
                code, strategy_id, adjust_type,
                kline: [{date, open, high, low, close, volume}, ...],
                signals: [{date, buy, sell}, ...],  # 仅命中 bar
                latest_is_buy: bool,  # 最新一根 bar 是否买点
                latest_is_sell: bool,
                latest_date: str,
                indicators: {main: [...], sub: [...]},  # 策略声明的叠加指标
            }
        """
        params = params or {}
        start_date, end_date = self._resolve_dates(start_date, end_date, days)

        # 1. 加载策略类
        strategy_cls = get_strategy_class(strategy_id)

        # 2. 加载数据（与 build_run_kline 同口径：auto_heal=False）
        df = _load_stock_daily(code, start_date, end_date, auto_heal=False)
        if df is None or df.empty:
            return self._empty_result(code, strategy_id, adjust_type)

        # 复权处理
        if adjust_type in ('qfq', 'hfq'):
            df = _apply_adjustment(df, code, adjust_type)

        # 截取最近 days 根（start_date 缺省时）
        if not start_date or len(df) > days:
            df = df.tail(days).copy()

        # 3. 构建 kline（用复权后的 df，trade_date 还是列）
        kline = self._build_kline(df)

        # 4. 跑 backtrader scan 模式
        signals = self._run_scan(strategy_cls, df, params)

        # 5. 判断最新 bar 是否买点
        latest_date = kline[-1]['date'] if kline else ''
        latest_is_buy = any(s['date'] == latest_date and s['buy'] for s in signals)
        latest_is_sell = any(s['date'] == latest_date and s['sell'] for s in signals)

        # 6. 策略声明的叠加指标
        indicators = self._build_indicators(strategy_cls, df, params)

        return {
            'code': code,
            'strategy_id': strategy_id,
            'adjust_type': adjust_type,
            'kline': kline,
            'signals': signals,
            'latest_is_buy': latest_is_buy,
            'latest_is_sell': latest_is_sell,
            'latest_date': latest_date,
            'indicators': indicators,
        }

    # -----------------------------------------------------------
    # 内部方法
    # -----------------------------------------------------------

    @staticmethod
    def _resolve_dates(start_date: Optional[str], end_date: Optional[str],
                       days: int) -> tuple:
        """解析日期区间，缺省时按 days 回溯"""
        today = datetime.now()
        if not end_date:
            end_date = today.strftime('%Y-%m-%d')
        if not start_date:
            # 回溯 days * 1.6 个自然日，覆盖周末/节假日
            start = today - timedelta(days=int(days * 1.6))
            start_date = start.strftime('%Y-%m-%d')
        return start_date, end_date

    @staticmethod
    def _build_kline(df: pd.DataFrame) -> List[Dict[str, Any]]:
        """从 DataFrame 构建 kline 数据（与 build_run_kline 一致）"""
        kline = []
        for _, r in df.iterrows():
            kline.append({
                'date': str(r['trade_date'])[:10],
                'open': round(float(r['open']), 2),
                'high': round(float(r['high']), 2),
                'low': round(float(r['low']), 2),
                'close': round(float(r['close']), 2),
                'volume': int(r['volume']) if pd.notna(r['volume']) else 0,
            })
        return kline

    @staticmethod
    def _run_scan(strategy_cls, df: pd.DataFrame,
                  params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """用 backtrader 跑 scan 模式，返回信号日志"""
        # 复制 df 避免 _normalize_columns 修改原 df（kline 和 indicators 用原 df）
        bt_df = df.copy()
        bt_df = _normalize_columns(bt_df)

        data = bt.feeds.PandasData(
            dataname=bt_df,
            datetime=None,
            open='open', high='high', low='low', close='close',
            volume='volume', openinterest=-1,
        )

        cerebro = bt.Cerebro()
        cerebro.adddata(data)
        # _scan_mode=True → on_bar 只记录不交易；只传策略自身参数
        cerebro.addstrategy(strategy_cls, _scan_mode=True, **params)
        cerebro.broker.setcash(1_000_000)  # scan 不交易，金额无实际意义

        results = cerebro.run()
        strat = results[0]
        return strat.get_signal_log()

    @staticmethod
    def _build_indicators(strategy_cls, df: pd.DataFrame,
                          params: Dict[str, Any]) -> dict:
        """构建策略声明的叠加指标"""
        try:
            from server.chart_utils import build_chart_indicators
            decls = strategy_cls.get_chart_indicators(params)
            return build_chart_indicators(df, decls)
        except Exception as e:
            logger.warning(f'构建策略指标失败: {e}')
            return {'main': [], 'sub': []}

    @staticmethod
    def _empty_result(code: str, strategy_id: str, adjust_type: str) -> dict:
        return {
            'code': code,
            'strategy_id': strategy_id,
            'adjust_type': adjust_type,
            'kline': [],
            'signals': [],
            'latest_is_buy': False,
            'latest_is_sell': False,
            'latest_date': '',
            'indicators': {'main': [], 'sub': []},
        }

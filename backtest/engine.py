"""回测引擎

封装 Backtrader Cerebro，提供统一的回测执行接口。
负责：
- 加载数据（通过 data_adapter）
- 配置策略、资金、佣金
- 运行回测
- 提取结果（统计指标、净值曲线、交易记录）
- 保存到 backtest.db
"""
import json
import uuid
import logging
import backtrader as bt
import pandas as pd
from datetime import datetime, date
from typing import Dict, Any, Optional, Type, Callable

from backtest.data_adapter import BacktraderDataFeed
from backtest.a_stock_broker import setup_broker
from backtest.strategies import get_strategy_class
from backtest.analyzers import NeutralBandAnalyzer
from utils.cache.backtest_db import (
    create_backtest_run, update_backtest_run,
    save_backtest_result, save_backtest_trades,
)

logger = logging.getLogger(__name__)


class BacktestEngine:
    """回测引擎"""

    def run(self, config: Dict[str, Any], run_id: str = None,
            progress_callback: Callable = None) -> Dict[str, Any]:
        """执行回测

        Args:
            config: 回测配置
            run_id: 可选，由调用方提供（API 已创建记录时传入）
            progress_callback: 可选，进度回调函数 (current, total, pct)

        Returns:
            回测结果字典
        """
        # run_id 由调用方提供，或自动生成
        now = datetime.now()
        if not run_id:
            run_id = f'BT-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'

        # 策略类查找
        strategy_id = config['strategy_id']
        strategy_cls = get_strategy_class(strategy_id)
        config['strategy_name'] = strategy_cls.__name__

        # 初始化日志
        from utils.task_logger import TaskLogger
        tlog = TaskLogger(run_id)
        tlog.task_start(f'backtest:{strategy_id}', config)

        try:
            update_backtest_run(run_id, status='running', started_at=now.strftime('%Y-%m-%d %H:%M:%S'))

            # 0. 交易日历校正日期
            warnings = _adjust_dates_by_calendar(config, tlog)

            tlog.info(f'加载数据: {config["code"]} ({config["start_date"]} ~ {config["end_date"]})')

            # 1. 加载数据（含数据自愈）
            data = BacktraderDataFeed.from_stock_daily(
                code=config['code'],
                start_date=config['start_date'],
                end_date=config['end_date'],
                adjust=config.get('adjust_type', 'qfq'),
            )

            # 1.5 资金校验：用回测区间第一天的价格估算
            asset_type = config.get('asset_type', 'stock')
            initial_cash = config.get('initial_cash', 100000)
            lot_size = config.get('lot_size', 100)
            df_check = data.p.dataname if hasattr(data, 'p') else None
            if df_check is not None and hasattr(df_check, 'close') and 'close' in df_check.columns:
                # 只取回测区间内的数据
                start = pd.Timestamp(config.get('start_date', '2000-01-01'))
                end = pd.Timestamp(config.get('end_date', '2099-12-31'))
                if hasattr(df_check.index, 'to_series'):
                    mask = (df_check.index >= start) & (df_check.index <= end)
                    period_data = df_check.loc[mask, 'close']
                    first_price = float(period_data.iloc[0]) if len(period_data) > 0 else 0
                else:
                    first_price = float(df_check['close'].iloc[0]) if len(df_check) > 0 else 0
            else:
                first_price = 0

            # 板块/指数回测跳过资金校验（不可直接交易，无"1手"概念）
            if asset_type not in ('board', 'index') and first_price > 0 and lot_size > 0:
                min_cost = first_price * lot_size
                if initial_cash < min_cost:
                    raise ValueError(
                        f'资金不足: 初始资金 {initial_cash:,.0f} 元 < 1手成本 {min_cost:,.0f} 元 '
                        f'({lot_size}股 x {first_price:,.2f}元)，请增加资金或选择更便宜的股票'
                    )

            # 2. 配置 Cerebro
            cerebro = bt.Cerebro()
            cerebro.adddata(data)

            # 3. 配置策略参数
            strategy_params = config.get('params', {})
            # 合并 A 股规则参数
            strategy_params.update({
                't_plus_1': config.get('t_plus_1', True),
                'lot_size': config.get('lot_size', 100),
                'stop_loss': config.get('stop_loss', 0),
                'take_profit': config.get('take_profit', 0),
                'trailing_stop': config.get('trailing_stop', 0),
                'position_mode': config.get('position_mode', 'full'),
                'position_size': config.get('position_size', 1.0),
            })
            cerebro.addstrategy(strategy_cls, **strategy_params)

            # 4. 配置经纪商
            setup_broker(cerebro, config)

            # 5. 添加分析器
            cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.03)
            cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
            cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
            cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
            cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='time_return')
            cerebro.addanalyzer(NeutralBandAnalyzer, _name='neutral_band',
                                neutral_band_pct=config.get('neutral_band_pct', 2.0))

            # 5.5 进度观察者（如果有回调）
            total_bars = len(data) if hasattr(data, '__len__') else 0
            if progress_callback and total_bars > 0:
                cerebro.addobserver(
                    ProgressObserver,
                    total_bars=total_bars,
                    callback=progress_callback,
                )

            # 6. 运行
            initial_cash = config.get('initial_cash', 100000)
            tlog.info(f'开始回测: {config["code"]} | 策略: {strategy_id} | 资金: {initial_cash:,.0f}')
            results = cerebro.run()
            strategy = results[0]

            # 7. 提取结果
            final_value = cerebro.broker.getvalue()
            total_return = (final_value - initial_cash) / initial_cash

            # 提取分析器结果
            sharpe = strategy.analyzers.sharpe.get_analysis()
            drawdown = strategy.analyzers.drawdown.get_analysis()
            returns = strategy.analyzers.returns.get_analysis()
            trade_analysis = strategy.analyzers.trades.get_analysis()
            time_return = strategy.analyzers.time_return.get_analysis()

            # 计算统计指标
            neutral_band = strategy.analyzers.neutral_band.get_analysis()
            stats = _calculate_stats(
                sharpe=sharpe, drawdown=drawdown, returns=returns,
                trade_analysis=trade_analysis, time_return=time_return,
                initial_cash=initial_cash, final_value=final_value,
                start_date=config['start_date'], end_date=config['end_date'],
                neutral_band=neutral_band,
            )

            # 7.5 基准对比（沪深300）
            benchmark_code = config.get('benchmark', '000300')
            benchmark_return = _calc_benchmark_return(
                benchmark_code, config['start_date'], config['end_date'], tlog
            )
            if benchmark_return is not None:
                stats['benchmark_return'] = round(benchmark_return, 4)
                stats['excess_return'] = round(stats['total_return'] - benchmark_return, 4)
                tlog.info(f'基准收益: {benchmark_return*100:.2f}%, 超额收益: {stats["excess_return"]*100:.2f}%')

            # 提取曲线数据（计算一次，存变量复用）
            equity_curve = _extract_equity_curve(time_return, initial_cash)
            drawdown_curve = _extract_drawdown_curve(time_return, initial_cash)
            monthly_returns = _calculate_monthly_returns(time_return)

            # 修正 peak_equity：从净值曲线取最大值（比 _calculate_stats 的启发式更准确）
            stats['peak_equity'] = max(p['equity'] for p in equity_curve) if equity_curve else initial_cash

            # 提取交易记录
            trades = strategy.get_trades()
            tlog.info(f'回测完成: 交易{len(trades)}次, 收益率{stats.get("total_return",0)*100:.2f}%, 最终资产{final_value:,.0f}')

            # 8. 保存结果
            result_data = {
                **stats,
                'equity_curve_json': json.dumps(equity_curve, ensure_ascii=False),
                'drawdown_curve_json': json.dumps(drawdown_curve, ensure_ascii=False),
                'monthly_returns_json': json.dumps(monthly_returns, ensure_ascii=False),
            }
            save_backtest_result(run_id, result_data)
            save_backtest_trades(run_id, trades)

            # 更新任务状态
            duration = (datetime.now() - now).total_seconds()
            update_backtest_run(
                run_id,
                status='completed',
                completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                duration_seconds=round(duration, 2),
            )

            # 构建警告信息
            warnings = []
            if stats.get('trade_count', 0) == 0:
                warnings.append('零交易：未产生任何买卖信号，或资金不足无法开仓')
            if first_price > 0 and initial_cash < first_price * lot_size:
                warnings.append(f'资金不足：初始资金 {initial_cash:,.0f} 元不够买 1 手 ({lot_size}股 x {first_price:,.2f}元)')

            duration = (datetime.now() - now).total_seconds()
            tlog.task_end('completed', duration, {
                'trades': len(trades),
                'return': f'{stats.get("total_return",0)*100:.2f}%',
                'final_equity': f'{final_value:,.0f}',
            })

            return {
                'run_id': run_id,
                'status': 'completed',
                'warnings': warnings,
                **stats,
                'trades': trades,
                'equity_curve': equity_curve,
                'drawdown_curve': drawdown_curve,
                'monthly_returns': monthly_returns,
            }

        except Exception as e:
            update_backtest_run(
                run_id,
                status='failed',
                error_message=str(e),
                completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
            raise


def _calculate_stats(sharpe, drawdown, returns, trade_analysis,
                     time_return, initial_cash, final_value,
                     start_date, end_date, neutral_band=None) -> Dict[str, Any]:
    """计算统计指标"""
    # 收益
    total_return = (final_value - initial_cash) / initial_cash
    annual_return = returns.get('rnorm100', 0) / 100 if returns.get('rnorm100') else 0

    # 风险
    sharpe_ratio = sharpe.get('sharperatio', 0)
    max_drawdown = drawdown.get('max', {}).get('drawdown', 0) / 100 if drawdown.get('max') else 0

    # 交易统计（含中性带宽）
    total_trades = trade_analysis.get('total', {}).get('total', 0)
    won = trade_analysis.get('won', {}).get('total', 0)
    lost = trade_analysis.get('lost', {}).get('total', 0)

    # 中性带宽：从 analyzer 获取中性交易数
    neutral_count = 0
    if neutral_band:
        neutral_count = neutral_band.get('neutral_count', 0)
        # 重新计算胜率：排除中性交易
        effective_trades = total_trades - neutral_count
        win_rate = won / effective_trades if effective_trades > 0 else 0
    else:
        win_rate = won / total_trades if total_trades > 0 else 0

    # 盈亏比
    avg_won = trade_analysis.get('won', {}).get('pnl', {}).get('average', 0)
    avg_lost = abs(trade_analysis.get('lost', {}).get('pnl', {}).get('average', 0) or 1)
    profit_factor = avg_won / avg_lost if avg_lost > 0 else 0

    # 平均持仓天数
    avg_holding = trade_analysis.get('len', {}).get('average', 0)

    # 连续盈亏
    max_consecutive_wins = trade_analysis.get('streak', {}).get('won', {}).get('longest', 0)
    max_consecutive_losses = trade_analysis.get('streak', {}).get('lost', {}).get('longest', 0)

    # Calmar 比率
    calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

    return {
        'total_return': round(total_return, 4),
        'annual_return': round(annual_return, 4),
        'sharpe_ratio': round(sharpe_ratio, 4) if sharpe_ratio else 0,
        'max_drawdown': round(max_drawdown, 4),
        'calmar_ratio': round(calmar_ratio, 4),
        'trade_count': total_trades,
        'win_count': won,
        'loss_count': lost,
        'win_rate': round(win_rate, 4),
        'profit_factor': round(profit_factor, 4),
        'avg_win': round(avg_won, 2),
        'avg_loss': round(avg_lost, 2),
        'avg_holding_days': round(avg_holding, 1) if avg_holding else 0,
        'max_consecutive_wins': max_consecutive_wins,
        'max_consecutive_losses': max_consecutive_losses,
        'final_equity': round(final_value, 2),
        'peak_equity': round(initial_cash * (1 + max(abs(total_return), abs(max_drawdown))), 2),
        'neutral_count': neutral_count,
    }


def _extract_equity_curve(time_return, initial_cash) -> list:
    """提取净值曲线"""
    curve = []
    cumulative = initial_cash
    for date, ret in sorted(time_return.items()):
        cumulative *= (1 + ret)
        curve.append({
            'date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'equity': round(cumulative, 2),
        })
    return curve


def _extract_drawdown_curve(time_return, initial_cash) -> list:
    """提取回撤曲线"""
    curve = []
    cumulative = initial_cash
    peak = initial_cash
    for date, ret in sorted(time_return.items()):
        cumulative *= (1 + ret)
        if cumulative > peak:
            peak = cumulative
        drawdown = (cumulative - peak) / peak if peak > 0 else 0
        curve.append({
            'date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'drawdown': round(drawdown * 100, 2),
        })
    return curve


def _calculate_monthly_returns(time_return) -> dict:
    """计算月度收益"""
    monthly = {}
    for date, ret in sorted(time_return.items()):
        year = date.strftime('%Y') if hasattr(date, 'strftime') else str(date)[:4]
        month = int(date.strftime('%m')) if hasattr(date, 'strftime') else int(str(date)[5:7])
        if year not in monthly:
            monthly[year] = [0.0] * 12
        monthly[year][month - 1] += ret * 100

    # 四舍五入
    for year in monthly:
        monthly[year] = [round(v, 2) for v in monthly[year]]

    return monthly


def _calc_benchmark_return(benchmark_code: str, start_date: str, end_date: str,
                           tlog=None) -> Optional[float]:
    """计算基准指数在回测区间的收益率

    从 index_daily 表读取数据，计算区间收益率。
    """
    try:
        from utils.cache.market_data_db import get_index_daily
        df = get_index_daily(benchmark_code, start_date, end_date)
        if df.empty or len(df) < 2:
            if tlog:
                tlog.info(f'基准数据不足: {benchmark_code} ({len(df) if not df.empty else 0}条)')
            return None
        first_close = float(df.iloc[0]['close'])
        last_close = float(df.iloc[-1]['close'])
        if first_close <= 0:
            return None
        return (last_close - first_close) / first_close
    except Exception as e:
        logger.warning(f'基准收益计算失败: {e}')
        if tlog:
            tlog.info(f'基准收益计算失败: {e}')
        return None


def _adjust_dates_by_calendar(config: Dict[str, Any], tlog=None) -> list:
    """用交易日历校正回测起止日期

    非交易日自动向前取最近交易日。返回警告列表。
    """
    warnings = []
    try:
        from utils.trading_calendar import is_market_open, get_next_trading_date
        from datetime import timedelta

        start = datetime.strptime(config['start_date'], '%Y-%m-%d').date()
        end = datetime.strptime(config['end_date'], '%Y-%m-%d').date()

        # 校正 start_date：非交易日向后取最近交易日
        if not is_market_open('cn', start):
            new_start = get_next_trading_date('cn', start)
            if tlog:
                tlog.info(f'起始日非交易日 {config["start_date"]}，校正为 {new_start}')
            config['start_date'] = new_start.strftime('%Y-%m-%d')

        # 校正 end_date：非交易日向前取最近交易日
        if not is_market_open('cn', end):
            # 向前找最近交易日
            d = end - timedelta(days=1)
            while not is_market_open('cn', d) and d > start:
                d -= timedelta(days=1)
            if tlog:
                tlog.info(f'结束日非交易日 {config["end_date"]}，校正为 {d}')
            config['end_date'] = d.strftime('%Y-%m-%d')

        # 检查区间是否过长（超过3年）
        days = (end - start).days
        if days > 1095:
            warnings.append(f'回测区间较长: {days}天（约{days//365}年），可能影响性能')

    except Exception as e:
        logger.debug(f'交易日历校正跳过: {e}')
    return warnings


class ProgressObserver(bt.Observer):
    """自定义观察者，上报回测进度"""
    lines = ('progress',)
    plotinfo = dict(plot=False)

    params = (('total_bars', 0), ('callback', None),)

    def next(self):
        if self.p.callback and self.p.total_bars > 0:
            current = len(self.data)
            pct = min(current / self.p.total_bars, 1.0)
            self.p.callback(current, self.p.total_bars, pct)

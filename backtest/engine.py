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
from typing import Dict, Any, List, Optional, Type, Callable

from config import Config
from backtest import silence_backtrader_warnings
from backtest.data_adapter import BacktraderDataFeed
from backtest.a_stock_broker import setup_broker
from backtest.strategies import get_strategy_class
from backtest.strategy_base import TIMING_LABELS, normalize_timing
from backtest.analyzers import NeutralBandAnalyzer
from utils.cache.backtest_db import (
    create_backtest_run, update_backtest_run, get_backtest_run,
    save_backtest_result, save_backtest_trades,
)

logger = logging.getLogger(__name__)

# 指标口径版本（写进 strategy_run.engine_version）。
# 2026-10-18：年化由 rnorm100（按 bar 数 252/年）改为自然日 CAGR；非日线数据年化置空。
# 口径变化后历史 run 的数字不可直接与新结果比较，靠这个标记区分。
ENGINE_VERSION = "2026-10-18.calendar-annual"


class BacktestEngine:
    """回测引擎"""

    def run(self, config: Dict[str, Any], run_id: str = None,
            progress_callback: Callable = None,
            origin: str = 'manual', ref_id: str = None) -> Dict[str, Any]:
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

        # 账本兜底（设计文档 §3.2）：调用方忘了建 run 时由引擎自己建。
        # 不兜底的话 update_backtest_run 会更新 0 行（SQLite 不报错），
        # 而 save_backtest_result / save_backtest_trades 照样插入 → 孤儿结果。
        # 位置必须在 _adjust_dates_by_calendar 之前：资金校验失败时账上也必须有记录。
        if not get_backtest_run(run_id):
            create_backtest_run(run_id, config, origin=origin, ref_id=ref_id or '')
        elif origin != 'manual' or ref_id:
            update_backtest_run(run_id, origin=origin, ref_id=ref_id or '')

        # 初始化日志
        from utils.task_logger import TaskLogger
        tlog = TaskLogger(run_id)
        tlog.task_start(f'backtest:{strategy_id}', config)

        # 体检元信息：异常路径也要用，保证失败也有 health
        coverage_meta: Dict[str, Any] = {
            'data_bars': 0, 'expected_bars': 0, 'coverage_pct': 0.0, 'gaps': [], 'bars': 0,
        }

        try:
            update_backtest_run(run_id, status='running', started_at=now.strftime('%Y-%m-%d %H:%M:%S'))

            # 0. 交易日历校正日期
            warnings = _adjust_dates_by_calendar(config, tlog)

            tlog.info(f'加载数据: {config["code"]} ({config["start_date"]} ~ {config["end_date"]})')

            # 1. 加载数据（含数据自愈）
            # pre_close 推算占比（P0 §4.2c）——from_stock_daily 回填，用于 warnings 标注
            feed_stats: Dict[str, Any] = {}
            # v3 S4-B：策略里用到的因子 → 数据源补 f_<field> 线
            data = BacktraderDataFeed.from_stock_daily(
                code=config['code'],
                start_date=config['start_date'],
                end_date=config['end_date'],
                adjust=config.get('adjust_type', 'qfq'),
                stats=feed_stats,
                factor_fields=sorted(getattr(strategy_cls, '_factor_fields', ()) or ()),
            )

            # 1.5 资金校验：用回测区间第一天的价格估算
            asset_type = config.get('asset_type', 'stock')
            # ETF 涨跌停未模拟的口径声明（P0 §4.1）：ETF 限幅有 10% / 20% 两种，
            # 单靠代码前缀无法可靠区分，setup_broker 因此 fail-open（不拦）。
            # 必须在 warnings（实时）与 health_detail（落库）双落点写明，
            # 否则用户会把「一字板也能成交」误读成「已按涨跌停撮合」。
            etf_limit_note = ('ETF 涨跌停未模拟（10%/20% 无法可靠区分）'
                              if asset_type == 'etf' else '')
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

            # ---- 回测体检：数据覆盖率（设计文档 §4.3 判定链第 3 条的唯一写入点）----
            coverage_meta = _calc_coverage_meta(
                data=data,
                start_date=config['start_date'],
                end_date=config['end_date'],
                benchmark_code=config.get('benchmark', '000300'),
                tlog=tlog,
            )

            # 成交口径：**买 / 卖各自独立**，表达「尾盘买入、次日卖出」这类组合。
            #   entry_timing: next_open 次日开盘 / same_close 当日收盘（尾盘）
            #   exit_timing : next_open 次日开盘 / same_close 当日收盘 / next_close 次日收盘
            # 归一化在基类 normalize_timing，非法值回落 next_open。
            entry_timing = normalize_timing(config.get('entry_timing'), 'next_open')
            exit_timing = normalize_timing(
                config.get('exit_timing') or config.get('entry_timing'), 'next_open')

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
                'entry_timing': entry_timing,
                'exit_timing': exit_timing,
            })

            # 进度观察者要的总根数：backtrader feed 在 run() 之前 len() 恒为 0（未 preload），
            # 只能从源 DataFrame 取 —— 否则观察者根本挂不上，进度回调永远不触发。
            total_bars = 0
            try:
                total_bars = int(len(data.p.dataname.index))
            except Exception:
                total_bars = len(data) if hasattr(data, '__len__') else 0

            def _build_cerebro() -> bt.Cerebro:
                """装配一份全新的 Cerebro（向量化越界后要重跑，不能复用跑脏了的引擎）"""
                c = bt.Cerebro()
                c.adddata(data)
                c.addstrategy(strategy_cls, **strategy_params)
                setup_broker(c, config)
                # ⚠️ 这里绝不能再 broker 级set_coc(True)：coc 是 broker 全局开关、对买卖两端
                # 同时生效，一旦在这里打开，买卖分离就彻底失效。口径改由基类在每次下单前
                # 按entry_timing / exit_timing 单独设置。
                c.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', **SHARPE_ANALYZER_KWARGS)
                c.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
                c.addanalyzer(bt.analyzers.Returns, _name='returns')
                c.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
                c.addanalyzer(bt.analyzers.TimeReturn, _name='time_return')
                c.addanalyzer(NeutralBandAnalyzer, _name='neutral_band',
                              neutral_band_pct=config.get('neutral_band_pct', 0.1))
                if progress_callback and total_bars > 0:
                    c.addobserver(
                        ProgressObserver,
                        total_bars=total_bars,
                        callback=progress_callback,
                    )
                return c

            # 6. 运行
            initial_cash = config.get('initial_cash', 100000)
            tlog.info(f'开始回测: {config["code"]} | 策略: {strategy_id} | 资金: {initial_cash:,.0f}')
            # 刷屏治理：第三方库可能把 warnings 过滤器改宽，run 前重插一次抑制
            silence_backtrader_warnings()
            cerebro = _build_cerebro()
            try:
                results = cerebro.run()
            except IndexError as exc:
                # K 线根数 < 指标预热周期时，向量化（runonce）在 basicops.py 的
                # dst[i] = math.fsum(...) 上直接越界（存量缺陷：15 根数据跑 MA20）。
                # 逐根模式不会越界（指标未成形就不触发 next），跑完零交易，
                # 由体检判 insufficient_bars 而不是塌缩成 error。
                if 'index out of range' not in str(exc):
                    raise
                tlog.warning(f'向量化回测越界（K 线 {coverage_meta.get("bars") or total_bars} 根，'
                             f'疑似不足指标预热），改用逐根模式重跑: {exc}')
                cerebro = _build_cerebro()
                results = cerebro.run(runonce=False)
            strategy = results[0]
            # 策略指标预热所需的最小 K 线根数（backtrader 在策略实例化时算好）
            min_bars = int(getattr(strategy, '_minperiod', 0) or 0)

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
            # 实际行情跨度（首末 bar 的自然日）：年化按它算，不按 bar 数（见 _calculate_stats）
            span_days = 0
            try:
                _idx = data.p.dataname.index
                span_days = max(int((_idx[-1] - _idx[0]).days), 0) if len(_idx) >= 2 else 0
            except Exception:  # noqa: BLE001 - 跨度拿不到就退回旧口径，不能拖垮回测
                span_days = 0
            stats = _calculate_stats(
                sharpe=sharpe, drawdown=drawdown, returns=returns,
                trade_analysis=trade_analysis, time_return=time_return,
                initial_cash=initial_cash, final_value=final_value,
                start_date=config['start_date'], end_date=config['end_date'],
                neutral_band=neutral_band, span_days=span_days,
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
            # P1：分年拆解（纯函数聚合；落库列 yearly_stats_json 由迁移添加）
            from backtest.statistics import calculate_yearly_stats
            yearly_stats = calculate_yearly_stats(time_return, trades)
            tlog.info(f'回测完成: 交易{len(trades)}次, 收益率{stats.get("total_return",0)*100:.2f}%, 最终资产{final_value:,.0f}')

            # ---- 回测体检：health 判定（bad_strategy > insufficient_bars > data_gap > no_signal > ok）----
            from backtest.health import (resolve_health, MIN_TRADE_SAMPLE,
                                         build_execution_attribution)
            bars_count = int(coverage_meta.get('bars') or total_bars or 0)
            health, health_detail = resolve_health(
                coverage_pct=coverage_meta['coverage_pct'],
                trade_count=stats.get('trade_count', 0),
                unsupported=getattr(strategy_cls, '_unsupported_conditions', None),
                gap_count=len(coverage_meta['gaps']),
                min_coverage=_min_coverage(),
                bars=bars_count,
                min_bars=min_bars,
            )
            # bar 频率异常（月线/周线被当日线）：年化与夏普都不可信，必须在体检里写明，
            # 否则会像 2026-10-18 那次一样报出「16 根 bar 换来 383% 年化 + 夏普 5.03」。
            if stats.get('bar_frequency_abnormal'):
                _bf_note = (f'K 线频率异常：{bars_count} 根跨 {span_days} 天（非日线），'
                            f'年化与夏普不可信')
                health_detail = (f'{health_detail}；' if health_detail else '') + _bf_note

            # ---- P0 撮合保真归因（§4.6）：拒单/滑点翻成可读文案 ----
            _bk = getattr(cerebro, 'broker', None)
            _rule = getattr(_bk, 'rule_ctx', {}) or {}
            try:
                _attempts = strategy.get_order_attempts()
            except Exception:  # noqa: BLE001 - 归因是增强信息，拿不到就不显示
                _attempts = {}
            exec_attr = build_execution_attribution(
                getattr(_bk, 'blocked', {}) or {}, _attempts,
                _rule.get('limit_pct'), getattr(_bk, 'slippage_cost', 0.0))
            exec_notes = list(exec_attr) + ['数据口径：前复权（源侧预复权）']
            _pc_ratio = float(feed_stats.get('pre_close_imputed_ratio') or 0.0)
            if _pc_ratio > 0:
                exec_notes.append(f'{_pc_ratio * 100:.0f}% 的昨收为推算值，除权日涨跌幅可能失真')
            # 归因落 health_detail（§4.6 双落点：warnings 不落库，刷新后靠这里仍可见）
            if exec_attr:
                health_detail = (f'{health_detail}；' if health_detail else '') + '；'.join(exec_attr)
            # ETF 涨跌停未模拟：同样落 health_detail（刷新后仍可见），不静默
            if etf_limit_note:
                health_detail = (f'{health_detail}；' if health_detail else '') + etf_limit_note

            # 8. 保存结果
            result_data = {
                **stats,
                'equity_curve_json': json.dumps(equity_curve, ensure_ascii=False),
                'drawdown_curve_json': json.dumps(drawdown_curve, ensure_ascii=False),
                'monthly_returns_json': json.dumps(monthly_returns, ensure_ascii=False),
                'yearly_stats_json': json.dumps(yearly_stats, ensure_ascii=False),
                'health': health,
                'health_detail': health_detail,
                'data_bars': coverage_meta['data_bars'],
                'expected_bars': coverage_meta['expected_bars'],
                'coverage_pct': coverage_meta['coverage_pct'],
                'gaps_json': json.dumps(coverage_meta['gaps'], ensure_ascii=False),
                'trade_sample_ok': 1 if stats.get('trade_count', 0) >= MIN_TRADE_SAMPLE else 0,
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

            # 构建警告信息（保留 _adjust_dates_by_calendar 收集的警告）
            # 0 笔交易必须区分「没信号」与「钱不够被拒」：两者处置完全相反，
            # 混成一句话会让「全仓 + 次日开盘买入因跳空被Margin 拒单」这类问题
            # 一直查不出来（拒单不报错，回测照常「完成」）。
            reject_info = {}
            try:
                reject_info = strategy.get_reject_info() or {}
            except Exception:
                reject_info = {}
            n_rejected = int(reject_info.get('rejected_orders') or 0)

            if health == 'no_signal':
                if n_rejected > 0:
                    last = reject_info.get('last') or {}
                    warnings.append(
                        f'零交易：产生了 {n_rejected} 笔买单但全部被 broker 拒单（资金不足）。'
                        f'若买入口径是「{TIMING_LABELS.get(last.get("entry_timing", ""), "")}」，'
                        f'下一根 bar 跳空会让实际成交价高于估算价；建议把仓位从「全仓」改成'
                        f'「按比例」或调低比例。')
                else:
                    warnings.append('零交易：未产生任何买卖信号，或资金不足无法开仓')
            elif health == 'insufficient_bars':
                warnings.append(f'窗口过短：K 线 {bars_count} 根 < 策略预热所需 {min_bars} 根，'
                                f'指标未成形，回测结论不可用（请拉长回测区间或换用更快出信号的策略）')
            if first_price > 0 and initial_cash < first_price * lot_size:
                warnings.append(f'资金不足：初始资金 {initial_cash:,.0f} 元不够买 1 手 ({lot_size}股 x {first_price:,.2f}元)')
            warnings.append(f'成交口径：{TIMING_LABELS.get(entry_timing, entry_timing)}买入'
                            f' → {TIMING_LABELS.get(exit_timing, exit_timing)}卖出')
            # 费用口径透明化（选型清单 P0-#6）：佣金万三按「已含经手费/证管费」市场惯例近似
            if asset_type == 'etf':
                # ETF（场内基金）免印花税，费用口径不能沿用个股文案（否则用户会以为被收了万五）
                warnings.append('费用口径：佣金万三（含经手费/证管费近似）+ ETF 免印花税 + 过户费十万分之一')
            else:
                warnings.append('费用口径：佣金万三（含经手费/证管费近似）+ 印花税万五（卖出）+ 过户费十万分之一')
            # P0 归因 + 口径声明（§4.6 / §11.8.3）
            warnings.extend(exec_notes)
            # ETF 涨跌停未模拟（10%/20% 无法可靠区分）——与 health_detail 同文案
            if etf_limit_note:
                warnings.append(etf_limit_note)

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
                # 体检四件套：**必须**出现在返回值里（而不仅落库）。
                # 研发闭环靠 result['health'] 判「这一轮是不是空结果」；早先只在
                # result_data 里写库、返回值没带，导致 _run_segment 永远拿到空 health，
                # degenerate 判定退化成只看成交笔数（2026-10-19 真实 2 轮跑出来才发现）。
                'health': health,
                'health_detail': health_detail,
                'data_bars': coverage_meta['data_bars'],
                'expected_bars': coverage_meta['expected_bars'],
                'coverage_pct': coverage_meta['coverage_pct'],
                'trades': trades,
                'equity_curve': equity_curve,
                'drawdown_curve': drawdown_curve,
                'monthly_returns': monthly_returns,
            }

        except Exception as e:
            from backtest.health import classify_failure
            health, health_detail = classify_failure(e)
            try:
                save_backtest_result(run_id, {
                    'health': health,
                    'health_detail': health_detail[:500],
                    'data_bars': coverage_meta['data_bars'],
                    'expected_bars': coverage_meta['expected_bars'],
                    'coverage_pct': coverage_meta['coverage_pct'],
                    'gaps_json': json.dumps(coverage_meta['gaps'], ensure_ascii=False),
                    'trade_sample_ok': 0,
                })
            except Exception:
                logger.warning(f'写体检元信息失败: {run_id}', exc_info=True)
            update_backtest_run(
                run_id,
                status='failed',
                error_message=str(e),
                completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
            raise


# ⚠️ backtrader 的 SharpeRatio **默认 timeframe=TimeFrame.Years**：
#   一年期回测只有 1 个「年收益」样本 → 标准差无从计算 → sharperatio=None；
#   两年期也只有 2 个样本（算出来的是「两点夏普」，统计上没有意义）。
#   实测：默认配置下 200 根日线的普通买入持有也返回 None —— 也就是说 2026-10-14 之前
#   落库的 IS/OOS 夏普要么是 0（被 None→0 折过，见 _calculate_stats），要么是两点产物，
#   **都不能用来选优或判过拟合**（「样本外夏普 > 0」「IS/OOS 比 > 1.3」这类红线等于空转）。
#   正确配置：按日收益、年化、无风险利率取 0（backtrader 的 rf 年度→日度换算在本版本
#   会得出 -5000 级别的荒谬值；rf=0 的夏普与手算一致）。
SHARPE_ANALYZER_KWARGS = dict(riskfreerate=0.0, timeframe=bt.TimeFrame.Days, annualize=True)


def _calculate_stats(sharpe, drawdown, returns, trade_analysis,
                     time_return, initial_cash, final_value,
                     start_date, end_date, neutral_band=None,
                     span_days: int = 0) -> Dict[str, Any]:
    """计算统计指标

    ``span_days`` = 实际行情首末 bar 的自然日跨度。
    ⚠️ 年化**必须**按自然日算，不能按 bar 数算：backtrader 的 Returns 分析器固定按
    「252 期/年」放大 ``rnorm100``，一旦喂进来的不是日线（真机事故 2026-10-18：
    000300 被灌了 24 根月线），16 根 bar 会被当成 16 个交易日 ⇒ 10.52% 总收益被
    放大成 **383% 年化**、夏普 5.03。bar 频率与日历严重不符时年化直接给 None
    （「算不出来」比「算错」诚实），并置 ``bar_frequency_abnormal`` 供体检标注。
    """
    # 收益
    total_return = (final_value - initial_cash) / initial_cash
    annual_return = returns.get('rnorm100', 0) / 100 if returns.get('rnorm100') else 0
    bar_frequency_abnormal = 0
    if span_days > 0:
        bars = len(time_return) if time_return else 0
        if bars and bars < span_days / 7:
            # 月线/周线被当日内数据：任何年化都是假的
            annual_return = None
            bar_frequency_abnormal = 1
        elif total_return > -1:
            annual_return = (1 + total_return) ** (365.0 / span_days) - 1
        else:
            annual_return = None

    # 风险
    # ⚠️ backtrader 的 SharpeRatio 在「算不出来」时返回 None（收益序列无波动/样本不足）。
    # 早期写成 `round(x) if x else 0` —— 把「无法计算」静默变成「夏普 = 0（中性）」，
    # 后果有二：① 亏损策略在面板/卡片上看起来夏普中性（实测 OOS 手算 -0.05，却显示 0）；
    # ② 研发选优把「算不出来」当 0 分，反而压过真正算出来的负夏普（SR-20261014-01F173
    # 第 1 轮就是这样当选 best 的）。保留 None，让下游按「未知」处理（UI 显示 —、选优排除）。
    _sharpe_raw = sharpe.get('sharperatio')
    sharpe_ratio = float(_sharpe_raw) if isinstance(_sharpe_raw, (int, float)) else None
    max_drawdown = drawdown.get('max', {}).get('drawdown', 0) / 100 if drawdown.get('max') else 0

    # 交易统计（含中性带宽）
    total_trades = trade_analysis.get('total', {}).get('total', 0)
    won = trade_analysis.get('won', {}).get('total', 0)
    lost = trade_analysis.get('lost', {}).get('total', 0)

    # 中性带宽：从 analyzer 获取中性交易数
    # ⚠️ 有效样本为 0 时胜率给 None，**不能给 0**（2026-10-18 事故）：
    # 「尾盘买入次日卖出」这类 1 日持有策略，每笔收益几乎都落在默认 ±2% 中性带内，
    # effective=0 → 以前返回 0，把「无从判断」冒充成「胜率 0%」。实测 300 只成分股
    # 全部报 0%，其中还有总收益 +46% 的标的，自相矛盾。口径同 sharpe：算不出来就是 None。
    # 胜率口径（用户 2026-10-18 拍板，写死）：
    #   收益 > +0.1% 算赢、< -0.1% 算输，±0.1% 以内视为噪声不计入。
    # 由 NeutralBandAnalyzer 同时算分子（wins）与分母（effective），口径天然一致。
    # ⚠️ 别再用「全部盈利笔数 ÷ 扣掉中性后的有效笔数」——分子分母不同源，实测 21/1=21.0。
    # ⚠️ 中性带默认值已从 2.0 收到 0.1：±2% 会把 1 日持有策略的所有交易吃光。
    neutral_count = 0
    neutral_rate = None
    effective_trades = total_trades
    if neutral_band:
        neutral_count = neutral_band.get('neutral_count', 0)
        neutral_rate = neutral_band.get('neutral_rate')
        effective_trades = neutral_band.get('effective_total')
        if effective_trades is None:
            effective_trades = total_trades - neutral_count
        _wr_nb = neutral_band.get('win_rate')
        win_rate = float(_wr_nb) if isinstance(_wr_nb, (int, float)) else None
    else:
        win_rate = won / total_trades if total_trades > 0 else None

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
    if max_drawdown == 0:
        calmar_ratio = 0
    elif annual_return is None:
        calmar_ratio = None      # 年化算不出来 ≠ 0（同 sharpe 的 None 语义）
    else:
        calmar_ratio = annual_return / abs(max_drawdown)

    # P1 §4.2：日频统计（per-period SR / skew / kurt）——PSR/DSR 的标准输入。
    # ⚠️ 年化 sharpe_ratio 不能直接进 PSR 公式（公式要求 per-period，见 statistics 模块头）。
    # 对应落库列（sr_daily/skewness/kurtosis）由 P1 批次 2 的迁移添加。
    from backtest.statistics import daily_stats
    _ds = daily_stats(list(time_return.values())) if time_return else {}

    return {
        'total_return': round(total_return, 4),
        'annual_return': round(annual_return, 4) if annual_return is not None else None,
        'sharpe_ratio': round(sharpe_ratio, 4) if sharpe_ratio is not None else None,
        'max_drawdown': round(max_drawdown, 4),
        'calmar_ratio': round(calmar_ratio, 4) if calmar_ratio is not None else None,
        'bar_frequency_abnormal': bar_frequency_abnormal,
        'trade_count': total_trades,
        'win_count': won,
        'loss_count': lost,
        'win_rate': round(win_rate, 4) if win_rate is not None else None,
        'profit_factor': round(profit_factor, 4),
        'avg_win': round(avg_won, 2),
        'avg_loss': round(avg_lost, 2),
        'avg_holding_days': round(avg_holding, 1) if avg_holding else 0,
        'max_consecutive_wins': max_consecutive_wins,
        'max_consecutive_losses': max_consecutive_losses,
        'final_equity': round(final_value, 2),
        'peak_equity': round(initial_cash * (1 + max(abs(total_return), abs(max_drawdown))), 2),
        'neutral_count': neutral_count,
        'neutral_rate': round(neutral_rate, 4) if isinstance(neutral_rate, (int, float)) else None,
        'effective_trades': effective_trades,
        'sr_daily': round(_ds['sr_daily'], 6) if _ds.get('sr_daily') is not None else None,
        'skewness': round(_ds['skew'], 4) if _ds.get('skew') is not None else None,
        'kurtosis': round(_ds['kurt'], 4) if _ds.get('kurt') is not None else None,
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


def _min_coverage() -> float:
    """体检阈值：每次读取，便于运行期调整与测试打桩"""
    return float(getattr(Config, 'BACKTEST_MIN_COVERAGE', 0.95))


def _baseline_trading_days(benchmark_code: str, start_date: str, end_date: str,
                           tlog=None) -> List[str]:
    """基准交易日序列：优先用基准指数日线的真实交易日；拿不到则返回空列表，由调用方退化估算

    不用 utils/trading_calendar.is_market_open 枚举：exchange_calendars 不可用时它 fail-open 恒 True，
    会把每个自然日都当交易日，覆盖率被算成极低。
    """
    try:
        from utils.cache.market_data_db import get_index_daily
        df = get_index_daily(benchmark_code, start_date, end_date)
        if df is not None and not df.empty and 'trade_date' in df.columns:
            return sorted({str(day)[:10] for day in df['trade_date'].tolist()})
    except Exception as e:
        if tlog:
            tlog.info(f'覆盖率基准取指数日线失败，退化为工作日估算: {e}')
    return []


def _calc_coverage_meta(data, start_date: str, end_date: str,
                        benchmark_code: str = '000300', tlog=None) -> Dict[str, Any]:
    """算数据覆盖率与缺口区间（基准 = 基准指数真实交易日；拿不到则按工作日估算且不报缺口）"""
    from backtest.data_adapter import estimate_expected_days
    from backtest.health import calc_coverage
    try:
        actual = [stamp.strftime('%Y-%m-%d') for stamp in data.p.dataname.index]
    except Exception:
        actual = []
    baseline = _baseline_trading_days(benchmark_code, start_date, end_date, tlog)
    expected = estimate_expected_days(start_date, end_date)
    if baseline:
        # 本地指数日线可能只覆盖窗口的一段（真库 000300 只有近两年）：分母若只用这一段，
        # 覆盖率会被顶到 100%，数据缺口反而看不见。此时按基准的真实交易日密度把工作日
        # 估算校准到 A 股口径；只把「窗口尾部也在基准内」的短基准视为头部截断。
        try:
            tail_gap = (datetime.strptime(end_date[:10], '%Y-%m-%d')
                        - datetime.strptime(baseline[-1][:10], '%Y-%m-%d')).days
        except Exception:
            tail_gap = 0
        if (tail_gap <= 10 and expected and len(baseline) < 0.9 * expected
                and baseline[0] > start_date):
            span_expected = estimate_expected_days(baseline[0], baseline[-1])
            density = (len(baseline) / span_expected) if span_expected else 1.0
            calibrated = max(int(expected * density), len(baseline))
            if tlog:
                tlog.info(f'基准 {benchmark_code} 只覆盖 {baseline[0]}~{baseline[-1]}，覆盖率分母'
                          f'改用密度校准估算 {calibrated} 根（工作日估算 {expected} × {density:.3f}）')
            gaps = list(calc_coverage(actual, baseline).get('gaps', []))
            if baseline[0] > start_date:
                # 基准之外的头段按估算记一条缺口，前端「补这段数据」才有区间可补
                gaps.insert(0, {'start': start_date, 'end': baseline[0],
                                'missing': max(calibrated - len(baseline), 1)})
            return {'data_bars': len(actual), 'expected_bars': calibrated,
                    'coverage_pct': round(min(len(actual) / calibrated, 1.0), 4) if calibrated else 0.0,
                    'gaps': gaps, 'bars': len(actual)}
        meta = calc_coverage(actual, baseline)
        meta['bars'] = len(actual)
        return meta
    coverage = round(min(len(actual) / expected, 1.0), 4) if expected else 0.0
    return {'data_bars': len(actual), 'expected_bars': expected,
            'coverage_pct': coverage, 'gaps': [], 'bars': len(actual)}


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

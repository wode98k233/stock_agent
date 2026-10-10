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
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import backtrader as bt
import pandas as pd

from backtest.data_adapter import _load_stock_daily, _apply_adjustment, _normalize_columns
from backtest.strategies import get_strategy_class
from utils.cache.market_data_db import get_latest_trade_date, refresh_stale_tail
from utils.trading_calendar import get_effective_trading_date

logger = logging.getLogger(__name__)

# 预补尾**环状游标**：记住上一轮挑到 codes 的哪个位置，下一轮从那里往后接着挑。
#
# 为什么需要：已补成功的码下一轮不再落后、会自然轮换；但**持续失败**的码
# （数据源抖动、上游缺失）会一直留在落后面。若每轮都「从 codes 首位取前 N」，
# 这些码就会长期霸占名额，排在后面的码永远轮不到——2026-09-30 实测：
# 67/74 只落后、每轮只补 6 只，`513330` 排第 34 位，大屏长期显示「数据截止 09-29」。
_TAIL_CURSOR: Dict[str, int] = {"idx": 0}


def _pick_stale_codes(codes: List[str], effective: str, limit: int) -> List[str]:
    """按环状游标挑出最多 `limit` 个落后于 `effective` 的代码，并推进游标。

    游标按「扫描过的位置数」推进（而非挑中的数量），保证即使某段全是新鲜码
    也能继续往前走，不会卡死在同一段。
    """
    n = len(codes)
    if n == 0 or limit <= 0:
        return []
    start = _TAIL_CURSOR.get("idx", 0) % n
    picked: List[str] = []
    scanned = 0
    for k in range(n):
        c = codes[(start + k) % n]
        scanned = k + 1
        latest = get_latest_trade_date(c)
        if not latest or str(latest) < effective:
            picked.append(c)
            if len(picked) >= limit:
                break
    _TAIL_CURSOR["idx"] = (start + scanned) % n
    return picked


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

        # 0. 读取侧补尾：本地缓存落后于最近已完成交易日时先拉缺口（失败静默）
        try:
            refresh_stale_tail(code)
        except Exception:
            logger.debug("[scan] %s 尾部补齐异常（继续用本地缓存）", code, exc_info=True)

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

    def scan_many(self, codes, strategy_ids=None, *, days: int = 120,
                  adjust_type: str = 'qfq', max_workers: int = 4,
                  progress=None, refresh_stale_max: int = 0,
                  refresh_stale_budget_s: float = 0.0) -> Dict[str, Any]:
        """批量无状态信号扫描：K 线按 code 只加载一次，逐策略复用。

        Args:
            refresh_stale_max: 扫描前预补尾上限（码；0=关闭，既有调用方行为不变）。
                按环状游标对落后于最近已完成交易日的标的逐个补尾，失败静默。
            refresh_stale_budget_s: 补尾阶段的**时间上限**（秒；0=不限）。
                强烈建议调用方给一个有界值——补尾是网络密集的串行操作，
                不限时会让它吃光整个块预算，导致「块已判超时、worker 还在跑、
                进度事件迟到且最终结果被丢弃」。见下方注释。

        Returns:
            {code: {"hits": [{"strategy_id","date","latest"}],
                    "sells": [{"strategy_id","date","latest"}],
                    "latest_date": str, "closes": [近 5 日收盘], "error": str|None}}
            hits/sells 分别仅保留最近 5 根 bar 内的最近一次买/卖点；
            单个 code 失败不影响整批（失败分支 hits/sells 均为空列表）。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from backtest.strategies import get_all_strategies

        strategies = (list(strategy_ids) if strategy_ids is not None
                      else list(get_all_strategies().keys()))
        codes = list(dict.fromkeys(codes))
        start_date, end_date = self._resolve_dates(None, None, days)
        results: Dict[str, Any] = {}
        done = 0

        # 扫描前预补尾：最多 refresh_stale_max 个落后码（环状游标推进）。
        # 整体 try/except：补尾本身异常不得影响扫描主流程。
        if refresh_stale_max and refresh_stale_max > 0:
            try:
                effective = get_effective_trading_date("cn").isoformat()
                stale_codes = _pick_stale_codes(codes, effective, int(refresh_stale_max))
                n_stale = len(stale_codes)
                budget_t0 = time.monotonic()
                for i, c in enumerate(stale_codes, 1):
                    # 预算检查放在**每只之前**：宁可少补几只（环状游标保证下轮从
                    # 别处继续），也不能让补尾把块预算吃光——那样块会先判超时，
                    # 而 worker 仍在跑、进度迟到、结果被丢弃（2026-09-30 实测症状）。
                    if refresh_stale_budget_s and (time.monotonic() - budget_t0) > refresh_stale_budget_s:
                        if progress:
                            try:
                                progress(0, len(codes),
                                         f"补尾预算 {refresh_stale_budget_s:.0f}s 用尽，"
                                         f"{n_stale - i + 1} 只顺延")
                            except Exception:
                                pass
                        break
                    # ⚠️ 补尾必须发进度：这一段是**串行网络请求**（单只 1~3s，源抖动时更久），
                    # 原实现整段静默——用户看到的就是「大屏卡了几分钟、日志一行没有」，
                    # 随后才突然冒出扫描进度。
                    if progress:
                        try:
                            progress(0, len(codes), f"补尾 {i}/{n_stale} {c}")
                        except Exception:
                            pass
                    refresh_stale_tail(c)
            except Exception:
                logger.debug("[scan_many] 预补尾失败（继续扫描）", exc_info=True)

        def _one(code: str):
            try:
                df = _load_stock_daily(code, start_date, end_date, auto_heal=False)
                if df is None or df.empty:
                    return code, {"hits": [], "sells": [], "latest_date": "",
                                  "closes": [], "error": "无K线数据"}
                if adjust_type in ('qfq', 'hfq'):
                    df = _apply_adjustment(df, code, adjust_type)
                df = df.tail(days).copy()
                kline = self._build_kline(df)
                dates = [k.get("date") for k in kline]
                latest_date = dates[-1] if dates else ""
                window = set(dates[-5:])
                hits, sells = [], []
                for sid in strategies:
                    try:
                        sigs = self._run_scan(get_strategy_class(sid), df, {})
                    except Exception:
                        logger.debug("[scan_many] %s × %s 扫描异常（跳过）", code, sid,
                                     exc_info=True)
                        continue
                    hit = next((s for s in reversed(sigs) if s.get("buy")), None)
                    if hit and hit.get("date") in window:
                        hits.append({"strategy_id": sid, "date": hit["date"],
                                     "latest": hit["date"] == latest_date})
                    sell = next((s for s in reversed(sigs) if s.get("sell")), None)
                    if sell and sell.get("date") in window:
                        sells.append({"strategy_id": sid, "date": sell["date"],
                                      "latest": sell["date"] == latest_date})
                closes = [k.get("close") for k in kline[-5:] if k.get("close") is not None]
                return code, {"hits": hits, "sells": sells, "latest_date": latest_date,
                              "closes": closes, "error": None}
            except Exception as e:
                logger.warning("[scan_many] %s 扫描失败: %s", code, e)
                return code, {"hits": [], "sells": [], "latest_date": "",
                              "closes": [], "error": str(e)}

        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
            futures = [ex.submit(_one, c) for c in codes]
            for fut in as_completed(futures):
                code, payload = fut.result()
                results[code] = payload
                done += 1
                if progress:
                    try:
                        progress(done, len(codes), code)
                    except Exception:
                        pass
        return results

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
    def _min_bars_required(strategy_cls, params: Dict[str, Any]) -> int:
        """探测策略所需的指标最小周期（最小 K 线根数）。

        必须在 `runonce=False` 下用 1 根占位 K 线真实跑一次：
        - 未 run 时 `cerebro.strats[0][0][0]` 拿到的是 MetaStrategy 类而非实例，
          `_minperiod` 恒为 1（实测），据此判断会让守卫完全失效；
        - `runonce=False` 走向量 next() 模式，1 根 K 线也不会越界，可安全探测。
        探测失败返回 0（表示未知），调用方跳过预检、交由 backtrader 处理。
        """
        try:
            probe_df = pd.DataFrame({
                'trade_date': [datetime(2000, 1, 1)],
                'open': [1.0], 'high': [1.0], 'low': [1.0],
                'close': [1.0], 'volume': [0.0],
            })
            cerebro = bt.Cerebro(stdstats=False, runonce=False)
            cerebro.adddata(bt.feeds.PandasData(dataname=probe_df,
                                                datetime='trade_date'))
            cerebro.addstrategy(strategy_cls, _scan_mode=True, **params)
            strat = cerebro.run()[0]
            return int(getattr(strat, '_minperiod', 0) or 0)
        except Exception:
            logger.debug("[_run_scan] 探测最小周期失败（跳过预检）", exc_info=True)
            return 0

    @staticmethod
    def _run_scan(strategy_cls, df: pd.DataFrame,
                  params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """用 backtrader 跑 scan 模式，返回信号日志"""
        # 复制 df 避免 _normalize_columns 修改原 df（kline 和 indicators 用原 df）
        bt_df = df.copy()
        bt_df = _normalize_columns(bt_df)

        # 预检：K 线根数不足指标最小周期时直接返回空信号。
        # 原因：backtrader 默认 runonce=True 走向量化 once()，当 len(data) < minperiod
        # 时 SMA.once() 的 `dst[i] = ...` 会抛 IndexError（array assignment index
        # out of range），且在批量扫描中被 except 吞掉，表现为"该标的永远无信号"。
        # 指标未成型时任何读数都无意义，因此在此显式短路。
        min_bars = SignalScanner._min_bars_required(strategy_cls, params)
        if min_bars > 0 and len(bt_df) < min_bars:
            logger.debug(
                "[_run_scan] K线不足最小周期，跳过：len=%d < minperiod=%d",
                len(bt_df), min_bars,
            )
            return []

        data = bt.feeds.PandasData(
            dataname=bt_df,
            datetime=None,
            open='open', high='high', low='low', close='close',
            volume='volume', openinterest=-1,
        )

        cerebro = bt.Cerebro(stdstats=False)
        cerebro.adddata(data)
        # _scan_mode=True → on_bar 只记录不交易；只传策略自身参数
        cerebro.addstrategy(strategy_cls, _scan_mode=True, **params)
        cerebro.broker.setcash(1_000_000)  # scan 不交易，金额无实际意义

        # runonce=False：走 next() 逐 bar 模式，避免 once() 向量化在边界
        # （数据长度恰等于 minperiod）时的越界；实测信号值与 runonce=True 逐条一致。
        results = cerebro.run(runonce=False)
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

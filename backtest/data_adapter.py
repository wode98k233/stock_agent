"""回测数据适配器

从 market_data.db 读取 stock_daily 数据，转换为 backtrader DataFeed。
包含数据自愈机制：数据不足时自动补拉。

数据口径（2026-10-15 实证，P0 设计 §11.8.3）：stock_daily 全库为**源侧预复权**
（前复权快照）——适配器**不做**运行时复权；``adjust`` 参数仅作口径声明。
``pre_close`` 随 SELECT 带入 feed，供涨跌停判定（P0 §4.2）。
"""
import logging
import numpy as np
import pandas as pd
import backtrader as bt
from datetime import datetime
from typing import Optional, Sequence
from utils.cache.market_data_db import get_market_data_db

logger = logging.getLogger(__name__)


class AStockPandasData(bt.feeds.PandasData):
    """在 OHLCV 之上多带 ``pre_close`` / ``pct_change`` / ``amount`` / ``change_amount``。

    涨跌停判定需要昨收，而 Broker 内无法回看历史 bar，故随 feed 带入。
    ``pct_change`` / ``amount`` / ``change_amount`` 是策略 DSL 的公开数据字段
    （``json_compiler.DATA_REF_MAP``）——**声明了就必须真正接线**：此前只接了
    ``pre_close``，其余字段在运行时被 ``getattr(..., close)`` 静默替换成收盘价，
    导致「涨幅 > 5%」变成「价格 > 5 元」（2026-10-20 排查）。
    """

    lines = ('pre_close', 'pct_change', 'amount', 'change_amount')
    params = (
        ('pre_close', -1),
        ('pct_change', -1),
        ('amount', -1),
        ('change_amount', -1),
    )


#: feed 额外数据线的单一来源（因子 feed 动态建类时也要带上，否则会丢线）
EXTRA_DATA_LINES = ('pre_close', 'pct_change', 'amount', 'change_amount')
EXTRA_DATA_PARAMS = tuple((name, name) for name in EXTRA_DATA_LINES)


class BacktraderDataFeed:
    """从 stock_daily 表读取数据，转换为 backtrader DataFeed"""

    @staticmethod
    def from_stock_daily(code: str, start_date: str, end_date: str,
                         adjust: str = 'qfq',
                         stats: Optional[dict] = None,
                         factor_fields: Optional[Sequence[str]] = None
                         ) -> bt.feeds.PandasData:
        """创建 backtrader DataFeed

        Args:
            code: 股票代码（如 600519）
            start_date: 开始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
            adjust: **口径声明**。库内为源侧预复权（前复权快照）：
                qfq = 直通；none / hfq 无法从预复权数据还原，直通 + 警告
            stats: 可选输出字典——回填 ``pre_close_imputed_ratio``（推算占比），
                供回测 warnings / 归因使用（P0 §4.2(c)）

        Returns:
            bt.feeds.PandasData 实例
        """
        df = _load_stock_daily(code, start_date, end_date)

        if df.empty:
            raise ValueError(f'无数据: {code} ({start_date} ~ {end_date})')

        # 口径声明（P0 §11.8.3）：库内为源侧预复权数据，运行时复权已停用
        # （_apply_adjustment 对预复权数据会二重下调——600519 全窗口实测 -6.31%，
        # 且 latest_factor 取窗口末行会让同一 bar 的价格依赖回测窗口）。
        if adjust in ('none', 'hfq'):
            logger.warning(f'库内为前复权预复权数据，无法提供 {adjust} 口径（直通）: {code}')

        # pre_close 兜底（涨跌停判定基准）：真实值保留，缺失取上一根 close
        df, imputed_ratio = _fill_pre_close(df)
        # pct_change / change_amount 兜底：库里这两列在 ETF/部分源上为空，
        # 用 close/pre_close 推算（单位与库内一致：百分数，0.63 = 0.63%）。
        df = _fill_derived_fields(df)
        if stats is not None:
            stats['pre_close_imputed_ratio'] = imputed_ratio

        # 确保列名符合 backtrader 要求
        df = _normalize_columns(df)

        # 频率闸门（2026-10）：stock_daily 曾被写入 24 根月末快照（000300），
        # 非空即短路 ETF/指数兜底，回测把「月末买、次月卖」当「尾盘买、次日卖」。
        _assert_daily_frequency(df, code, start_date, end_date)

        # ── v3 S4-B：因子作为数据线（不是 Indicator 包装）──────────────
        # 编译器登记了 _factor_fields，这里一次性把 36 个因子里用到的算出来，
        # 作为 f_<field> 列挂到 feed 上。做成「线」而不是 Indicator 是为了避开
        # O(n²)：Indicator 每根 bar 重算一次全序列，几百根 bar 就爆。
        factor_fields = [f for f in (factor_fields or ()) if f]
        feed_cls = AStockPandasData
        feed_kwargs = {name: name for name in EXTRA_DATA_LINES}
        if factor_fields:
            from backtest.factor_lib import compute_factors
            fac = compute_factors(df, factor_fields)
            for fid in factor_fields:
                df[f'f_{fid}'] = fac[fid].to_numpy()
            feed_cls = type('FactorAStockPandasData', (AStockPandasData,), {
                'lines': EXTRA_DATA_LINES + tuple(f'f_{f}' for f in factor_fields),
                'params': EXTRA_DATA_PARAMS + tuple((f'f_{f}', f'f_{f}')
                                                    for f in factor_fields),
            })
            feed_kwargs.update({f'f_{f}': f'f_{f}' for f in factor_fields})

        # 创建 DataFeed（AStockPandasData 多带 pre_close 一条线）
        data = feed_cls(
            dataname=df,
            datetime=None,  # 使用 index 作为日期
            open='open',
            high='high',
            low='low',
            close='close',
            volume='volume',
            openinterest=-1,  # A 股无持仓量
            **feed_kwargs,
        )
        return data


def _index_name(code: str) -> Optional[str]:
    """index_info 里的指数名称；不是指数（或元数据不可用）返回 None。"""
    try:
        from utils.cache.market_data_db import get_index_name
        return get_index_name(code)
    except Exception:  # noqa: BLE001
        return None


def _is_non_daily(df: pd.DataFrame) -> bool:
    """相邻 bar 间隔中位数 > MAX_DAILY_GAP_DAYS → 不是日线。

    与 _assert_daily_frequency 同口径，但这里是**判定**（用来切数据源），
    不是**拒绝**（那个在 from_stock_daily 里抛错）。
    """
    if df is None or len(df) < 3:
        return False
    try:
        ds = pd.to_datetime(df['trade_date'], errors='coerce').dropna().sort_values()
        gaps = ds.diff().dt.days.dropna()
    except Exception:  # noqa: BLE001
        return False
    if gaps.empty:
        return False
    return float(gaps.median()) > MAX_DAILY_GAP_DAYS


def _is_index_symbol(code: str) -> bool:
    """该裸码是否应从 index_daily 取数（查 index_info，不靠前缀猜）。

    与 backtest 侧的图表口径同源：`000001` 既是上证指数也是平安银行，
    `resolve_symbol_market` 对同码取保守的 STOCK，只有纯指数码返回 INDEX。
    判定失败一律按 STOCK 放行（宁可走老路径，也不因元数据缺失拒跑）。
    """
    try:
        from utils.cache.market_data_db import resolve_symbol_market
        return resolve_symbol_market(code) == 'INDEX'
    except Exception as e:  # noqa: BLE001 - 元数据不可用不阻塞回测
        logger.warning(f'指数判定失败（按个股放行）: {code} - {e}')
        return False


def _normalize_index_df(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """把 index_daily 行补成 stock_daily 的形状。

    index_daily 没有 `pre_close`/`pct_change`：`pre_close` 置 NaN，交由
    `_fill_pre_close` 用上一根 close 推算（P0 §4.2(c) 的同一套兜底）。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if 'code' not in df.columns:
        df['code'] = code
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col not in df.columns:
            df[col] = np.nan
    if 'pre_close' not in df.columns:
        df['pre_close'] = np.nan
    if 'pct_change' not in df.columns:
        df['pct_change'] = np.nan
    if 'change_amount' not in df.columns:
        df['change_amount'] = np.nan
    if 'amount' not in df.columns:
        df['amount'] = 0.0
    df['trade_date'] = df['trade_date'].astype(str)
    keep = ['code', 'trade_date', 'open', 'high', 'low', 'close',
            'pre_close', 'pct_change', 'change_amount', 'volume', 'amount']
    return df[keep].sort_values('trade_date').reset_index(drop=True)


def _load_index_daily(code: str, start_date: str, end_date: str,
                      auto_heal: bool = True) -> pd.DataFrame:
    """从 index_daily 读指数日线；缺失且允许自愈时补拉一次。"""
    from utils.cache.market_data_db import get_index_daily

    try:
        df = get_index_daily(code, start_date, end_date)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'读 index_daily 失败: {code} - {e}')
        df = pd.DataFrame()

    if auto_heal and (df is None or df.empty):
        try:
            from utils.cache.market_data_db import fetch_and_store_index_daily
            days = _estimate_expected_days(start_date, end_date)
            logger.info(f'指数数据自愈: {code} 补拉 {days} 天')
            fetch_and_store_index_daily(code, days=days)
            df = get_index_daily(code, start_date, end_date)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'指数补拉失败: {code} - {e}')

    return _normalize_index_df(df, code)


def _load_stock_daily(code: str, start_date: str, end_date: str,
                      auto_heal: bool = True) -> pd.DataFrame:
    """从 stock_daily 表读取数据，支持数据自愈

    Args:
        code: 股票代码
        start_date: 开始日期
        end_date: 结束日期
        auto_heal: 数据不足时是否自动补拉

    Returns:
        DataFrame
    """
    # 指数分流（2026-10-18）：000300 这类裸码在 stock_daily 里可能只有月频快照
    # （mx_data 灌入的 24 根月末），而 index_daily 有完整日线。历史上靠「df 非空即
    # 短路」判断，于是 24 根月线被当日内数据回测（8 轮研发全废，见
    # backtest/strategy_research.py:68 事故复盘）。同码不同标的的判定统一走
    # resolve_symbol_market（000001 是同码个股，仍走 stock_daily）。
    if _is_index_symbol(code):
        df_index = _load_index_daily(code, start_date, end_date, auto_heal=auto_heal)
        if not df_index.empty:
            logger.info(f'指数取数走 index_daily: {code} {len(df_index)} 条')
            return df_index

    with get_market_data_db() as conn:
        sql = '''SELECT code, trade_date, open, high, low, close, pre_close, pct_change,
                        change_amount, volume, amount
                 FROM stock_daily
                 WHERE code = ? AND trade_date >= ? AND trade_date <= ?
                 ORDER BY trade_date'''
        df = pd.read_sql_query(sql, conn, params=(code, start_date, end_date))

    # 二次判定（2026-10-18）：000300 这类指数被当股票灌过，于是 stock_info 里也有行，
    # resolve_symbol_market 按「同码保守取 STOCK」会放行 → 走到这里。若该码在 index_info
    # 里存在、而股票表拿到的不是日线（月频快照），改走指数表。
    # 只在前两条同时成立时才切，因此 000001（平安银行，股票表是日线）不受影响。
    # 窗口内股票表直接为空时同样改走指数表（否则会一路落到「无数据」）。
    if _index_name(code) and (df.empty or _is_non_daily(df)):
        df_index = _load_index_daily(code, start_date, end_date, auto_heal=auto_heal)
        if len(df_index) > len(df):
            logger.info(f'股票表无日线数据（{len(df)} 条），改走 index_daily: {code} {len(df_index)} 条')
            return df_index

    # 数据自愈：数据量明显不足时自动补拉一次
    if auto_heal and _is_data_insufficient(df, start_date, end_date):
        logger.info(f'数据自愈: {code} 数据不足({len(df)}条)，尝试自动补拉')
        try:
            from utils.cache.market_data_db import fetch_and_store_daily
            fetch_and_store_daily(code, days=_estimate_expected_days(start_date, end_date))
            # 重新查询
            with get_market_data_db() as conn:
                df = pd.read_sql_query(sql, conn, params=(code, start_date, end_date))
            logger.info(f'数据自愈完成: {code} 补拉后 {len(df)} 条')
        except Exception as e:
            logger.warning(f'数据自愈失败: {code} - {e}')

    # ETF 兜底：股票接口无数据时尝试 ETF 接口
    if auto_heal and df.empty:
        logger.info(f'股票接口无数据，尝试 ETF 接口: {code}')
        df = _try_fetch_etf(code, start_date, end_date)

    # 指数兜底：ETF 也无数据时尝试指数接口（处理 000015 红利指数等）
    if auto_heal and df.empty:
        logger.info(f'ETF 接口无数据，尝试指数接口: {code}')
        df = _try_fetch_index(code, start_date, end_date)

    return df


def _try_fetch_etf(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """尝试从 ETF 接口获取数据并存入 stock_daily

    用于处理 000015(红利ETF) 等股票型代码的 ETF。
    优先东财 ETF API，失败则用新浪 ETF API。
    """
    try:
        from tools.fetcher.akshare_ds import AkshareDataSource
        AkshareDataSource._ensure_patch()
        import akshare as ak

        sd = start_date.replace('-', '')
        ed = end_date.replace('-', '')

        # 尝试1: 东财 ETF API
        df = None
        try:
            df = ak.fund_etf_hist_em(symbol=code, period='daily', adjust='', start_date=sd, end_date=ed)
        except Exception:
            pass

        # 尝试2: 新浪 ETF API（处理 000015 等股票型 ETF 代码）
        if df is None or df.empty:
            logger.info(f'东财 ETF 无数据，尝试新浪: {code}')
            try:
                # 新浪需要市场前缀：sz000015 / sh510300
                prefix = 'sh' if code.startswith(('5', '6', '9')) else 'sz'
                df = ak.fund_etf_hist_sina(symbol=f'{prefix}{code}')
                if df is not None and not df.empty:
                    # 新浪返回英文列名，直接可用
                    df = df.rename(columns={'date': 'trade_date'})
            except Exception as e:
                logger.warning(f'新浪 ETF 也失败: {code} - {e}')

        if df is None or df.empty:
            logger.info(f'所有 ETF 接口均无数据: {code}')
            return pd.DataFrame()

        # 标准化列名（东财返回中文，新浪返回英文）
        col_map = {'日期': 'trade_date', '开盘': 'open', '最高': 'high', '最低': 'low',
                   '收盘': 'close', '成交量': 'volume', '成交额': 'amount'}
        df = df.rename(columns=col_map)
        for col in ['trade_date', 'open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                logger.warning(f'ETF 数据缺少列 {col}: {code}')
                return pd.DataFrame()

        df['code'] = code
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
        if 'amount' not in df.columns:
            df['amount'] = 0.0

        # 存入 stock_daily
        from utils.cache.market_data_db import upsert_stock_daily
        records = df[['code', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount']].to_dict('records')
        upsert_stock_daily(records, source='akshare_etf')
        logger.info(f'ETF 数据已存入: {code}, {len(records)} 条')

        # 重新查询
        with get_market_data_db() as conn:
            sql = '''SELECT code, trade_date, open, high, low, close, volume, amount
                     FROM stock_daily
                     WHERE code = ? AND trade_date >= ? AND trade_date <= ?
                     ORDER BY trade_date'''
            return pd.read_sql_query(sql, conn, params=(code, start_date, end_date))

    except Exception as e:
        logger.warning(f'ETF 接口失败: {code} - {e}')
        return pd.DataFrame()


def _try_fetch_index(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """尝试从指数接口获取数据并存入 stock_daily

    用于处理 000015(红利指数) 等指数代码。
    尝试 sh 和 sz 两个前缀。
    """
    try:
        from tools.fetcher.akshare_ds import AkshareDataSource
        AkshareDataSource._ensure_patch()
        import akshare as ak

        # 指数需要市场前缀：sh000015 / sz399001
        df = None
        for prefix in ('sh', 'sz'):
            try:
                df = ak.stock_zh_index_daily_em(symbol=f'{prefix}{code}')
                if df is not None and not df.empty:
                    logger.info(f'指数接口成功: {prefix}{code}, {len(df)} 条')
                    break
            except Exception:
                continue

        if df is None or df.empty:
            logger.info(f'指数接口也无数据: {code}')
            return pd.DataFrame()

        # 标准化列名
        col_map = {'date': 'trade_date'}
        df = df.rename(columns=col_map)
        for col in ['trade_date', 'open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                logger.warning(f'指数数据缺少列 {col}: {code}')
                return pd.DataFrame()

        df['code'] = code
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
        if 'amount' not in df.columns:
            df['amount'] = 0.0

        # 过滤日期范围
        df = df[(df['trade_date'] >= start_date) & (df['trade_date'] <= end_date)]

        # 存入 stock_daily
        from utils.cache.market_data_db import upsert_stock_daily
        records = df[['code', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount']].to_dict('records')
        if records:
            upsert_stock_daily(records, source='akshare_index')
            logger.info(f'指数数据已存入: {code}, {len(records)} 条')

        # 重新查询
        with get_market_data_db() as conn:
            sql = '''SELECT code, trade_date, open, high, low, close, volume, amount
                     FROM stock_daily
                     WHERE code = ? AND trade_date >= ? AND trade_date <= ?
                     ORDER BY trade_date'''
            return pd.read_sql_query(sql, conn, params=(code, start_date, end_date))

    except Exception as e:
        logger.warning(f'指数接口失败: {code} - {e}')
        return pd.DataFrame()


def _is_data_insufficient(df: pd.DataFrame, start_date: str, end_date: str) -> bool:
    """判断数据是否明显不足

    规则：如果数据量少于预期交易日数的 50%，视为不足。
    """
    if df.empty:
        return True
    expected = _estimate_expected_days(start_date, end_date)
    return len(df) < expected * 0.5


def _estimate_expected_days(start_date: str, end_date: str) -> int:
    """估算区间内预期交易日数（粗略：自然日 * 5/7）"""
    try:
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        days = (end - start).days
        return max(int(days * 5 / 7), 1)
    except Exception:
        return 250


def estimate_expected_days(start_date: str, end_date: str) -> int:
    """按工作日估算区间内应有多少根 K 线（公开入口，供回测体检在拿不到基准指数时退化使用）"""
    return _estimate_expected_days(start_date, end_date)


def _apply_adjustment(df: pd.DataFrame, code: str, adjust: str) -> pd.DataFrame:
    """⚠️ **已停用（2026-10-15，P0 §11.8.3 实证）**：直通返回，不再复权。

    停用原因：``stock_daily`` 全库为**源侧预复权**（前复权快照），对本数据再做
    因子运算 = 二重下调（600519 全窗口实测 2024-01-02: 1550.60 → 1452.76，-6.31%）；
    且 ``latest_factor`` 取**回测窗口末行**因子，会让同一根 bar 的价格依赖窗口选择。
    函数保留仅为历史实现备查（原实现见 git 历史），默认路径一律不调用。
    """
    return df.copy()


def _fill_pre_close(df: pd.DataFrame) -> tuple:
    """兜底推算 ``pre_close``（涨跌停判定的基准，P0 §4.2(c)）。

    优先级：DB 真实值保留；缺失时取**上一根 close**（项目既有范式，对齐
    ``hithink_marketdb`` 的 shift 推算）；首行无上一根 → 保持 NaN。

    ⚠️ 数据为源侧预复权（§11.8.3），pre_close 与价格天然同口径，**无需再乘因子**。

    Returns:
        ``(df, imputed_ratio)``——推算行占全部行的比例（供回测 warnings 归因）。
    """
    n = len(df)
    if n == 0:
        return df, 0.0
    df = df.sort_values('trade_date').reset_index(drop=True)
    if 'pre_close' not in df.columns:
        df['pre_close'] = np.nan
    need = df['pre_close'].isna()
    prev_close = df['close'].shift(1)
    df['pre_close'] = df['pre_close'].fillna(prev_close)
    imputed = int((need & prev_close.notna()).sum())
    return df, (imputed / n if n else 0.0)


def _fill_derived_fields(df: pd.DataFrame) -> pd.DataFrame:
    """补齐 ``pct_change`` / ``change_amount`` / ``amount`` 列。

    为什么不在运行时算：DSL 的 ``{"data": "pct_change"}`` 必须取到一条真实的
    backtrader line，否则 ``_resolve_ref`` 只能判「不可用」。库里这两列在 ETF
    与部分数据源上大面积为空，所以在这里用 ``close/pre_close`` 推算；
    ``amount`` 无法推算（需要成交明细），缺失就保持 NaN，由条件侧 fail-closed。

    Returns:
        原地补列后的 DataFrame（不复制，调用方已持有副本语义）。
    """
    if df is None or df.empty:
        return df
    if 'pct_change' not in df.columns:
        df['pct_change'] = np.nan
    if 'change_amount' not in df.columns:
        df['change_amount'] = np.nan
    if 'amount' not in df.columns:
        df['amount'] = np.nan

    close = pd.to_numeric(df['close'], errors='coerce')
    pre_close = pd.to_numeric(df['pre_close'], errors='coerce')
    valid = pre_close > 0

    pct = pd.to_numeric(df['pct_change'], errors='coerce')
    computed_pct = (close / pre_close - 1.0) * 100.0
    df['pct_change'] = pct.where(pct.notna(), computed_pct.where(valid))

    chg = pd.to_numeric(df['change_amount'], errors='coerce')
    computed_chg = close - pre_close
    df['change_amount'] = chg.where(chg.notna(), computed_chg.where(valid))
    return df


# 相邻 bar 间隔中位数超过该值 → 非日线（周线 7 天 / 月线 28~31 天）。
# 用中位数而非最大值：周末（3 天）、春节长假（约 10 天）、停牌都会产生长间隔。
MAX_DAILY_GAP_DAYS = 5


def _assert_daily_frequency(df: pd.DataFrame, code: str,
                            start_date: str, end_date: str) -> None:
    """日线回测的频率闸门：月线/周线快照直接拒绝，不做静默降级。

    2026-10 事故：``stock_daily`` 的 ``000300`` 只有 24 根**月末** bar，
    适配器仅以 ``df.empty`` 判定是否需要 ETF/指数兜底 → 24 行非空即永久短路，
    回测把「月末收盘买、次月开盘卖」当成「尾盘买、次日卖」，还按个股 10%
    涨跌停撮合（一根月线涨 10.33% 被当成涨停拒单）。
    """
    if len(df) < 3:
        # 行数太少无统计意义，交给上层「无数据 / 行数不足」报错
        return
    dates = pd.to_datetime(pd.Series(list(df.index)), errors='coerce').dropna().sort_values()
    gaps = dates.diff().dt.days.dropna()
    if gaps.empty:
        return
    gap = float(gaps.median())
    if gap > MAX_DAILY_GAP_DAYS:
        raise ValueError(
            f'标的 {code} 在 {start_date}~{end_date} 的数据不是日线'
            f'（相邻间隔中位数 {gap:g} 天，共 {len(df)} 根）。'
            f'请勿用月线/周线快照做日线回测。'
        )


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """规范化列名，适配 backtrader"""
    # 设置 trade_date 为索引
    if 'trade_date' in df.columns:
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.set_index('trade_date')

    # 确保必要列存在
    required = ['open', 'high', 'low', 'close', 'volume']
    for col in required:
        if col not in df.columns:
            raise ValueError(f'缺少必要列: {col}')

    # 填充缺失值
    df['volume'] = df['volume'].fillna(0)

    # pre_close：缺失只降级为「不拦」（fail-open），**不进 required**（P0 §4.2(d)）
    if 'pre_close' not in df.columns:
        df['pre_close'] = np.nan

    return df


def get_user_positions(source: str = 'eastmoney', db_path: Optional[str] = None):
    """预留：读取用户真实持仓（东财同步快照），供回测对照/复盘使用。

    用途 a（策略对照）：回测报告附「你的实际持仓」vs「策略建议持仓」对照表
    用途 b（信号触达）：adopt=track 的信号触发时检查是否命中用户持仓 →
        推送文案升级为「你持仓中的 002714 触发买点」

    TODO(东财同步 v2.1 链路6)：本期只预留签名不实现（避免范围蔓延）。
    实现时只读 stock_radar.db 的 portfolio_position（source=? AND qty>0），
    返回 [{stock_code, stock_name, qty, avg_cost, avail_qty, etf_flag, updated_at}]；
    参考 server/storage.py::PortfolioStorage.list_positions 的口径。
    """
    raise NotImplementedError(
        "get_user_positions 为预留接口（东财同步链路6），本期不实现")

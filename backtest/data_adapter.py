"""回测数据适配器

从 market_data.db 读取 stock_daily 数据，转换为 backtrader DataFeed。
支持复权计算（前复权/后复权/不复权）。
包含数据自愈机制：数据不足时自动补拉。
"""
import logging
import pandas as pd
import backtrader as bt
from datetime import datetime
from typing import Optional
from utils.cache.market_data_db import get_market_data_db

logger = logging.getLogger(__name__)


class BacktraderDataFeed:
    """从 stock_daily 表读取数据，转换为 backtrader DataFeed"""

    @staticmethod
    def from_stock_daily(code: str, start_date: str, end_date: str,
                         adjust: str = 'qfq') -> bt.feeds.PandasData:
        """创建 backtrader DataFeed

        Args:
            code: 股票代码（如 600519）
            start_date: 开始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
            adjust: 复权方式 qfq(前复权) / hfq(后复权) / none(不复权)

        Returns:
            bt.feeds.PandasData 实例
        """
        df = _load_stock_daily(code, start_date, end_date)

        if df.empty:
            raise ValueError(f'无数据: {code} ({start_date} ~ {end_date})')

        # 复权处理
        if adjust in ('qfq', 'hfq'):
            df = _apply_adjustment(df, code, adjust)

        # 确保列名符合 backtrader 要求
        df = _normalize_columns(df)

        # 创建 DataFeed
        data = bt.feeds.PandasData(
            dataname=df,
            datetime=None,  # 使用 index 作为日期
            open='open',
            high='high',
            low='low',
            close='close',
            volume='volume',
            openinterest=-1,  # A 股无持仓量
        )
        return data


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
    with get_market_data_db() as conn:
        sql = '''SELECT code, trade_date, open, high, low, close, volume, amount
                 FROM stock_daily
                 WHERE code = ? AND trade_date >= ? AND trade_date <= ?
                 ORDER BY trade_date'''
        df = pd.read_sql_query(sql, conn, params=(code, start_date, end_date))

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


def _apply_adjustment(df: pd.DataFrame, code: str, adjust: str) -> pd.DataFrame:
    """应用复权因子

    前复权(qfq): 价格 × adjust_factor / 最新adjust_factor
    后复权(hfq): 价格 × adjust_factor
    """
    with get_market_data_db() as conn:
        adj_sql = '''SELECT trade_date, fore_adjust_factor, back_adjust_factor
                     FROM stock_adjust_factor
                     WHERE code = ?
                     ORDER BY trade_date'''
        adj_df = pd.read_sql_query(adj_sql, conn, params=(code,))

    if adj_df.empty:
        # 无复权因子，返回原始数据
        return df

    # 合并复权因子
    df = df.merge(adj_df, on='trade_date', how='left')

    # 填充缺失的复权因子（使用最近的有效值）
    if adjust == 'qfq':
        factor_col = 'fore_adjust_factor'
        # 前复权：用最新的因子作为基准
        latest_factor = df[factor_col].dropna().iloc[-1] if not df[factor_col].dropna().empty else 1.0
        df[factor_col] = df[factor_col].fillna(latest_factor)
        df[factor_col] = df[factor_col] / latest_factor  # 归一化到最新
    elif adjust == 'hfq':
        factor_col = 'back_adjust_factor'
        df[factor_col] = df[factor_col].fillna(1.0)
    else:
        return df.drop(columns=['fore_adjust_factor', 'back_adjust_factor'], errors='ignore')

    # 应用复权因子
    price_cols = ['open', 'high', 'low', 'close']
    for col in price_cols:
        if col in df.columns:
            df[col] = df[col] * df[factor_col]

    # 清理临时列
    df = df.drop(columns=['fore_adjust_factor', 'back_adjust_factor'], errors='ignore')

    return df


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

    return df

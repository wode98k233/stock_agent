"""Baostock 数据源"""
import logging
import os
import pandas as pd
from datetime import datetime, timedelta
from .base import DataSource
from .cache import get_baostock_cache

logger = logging.getLogger("radar.fetcher")


class BaostockDataSource(DataSource):
    name: str = "baostock"
    label: str = "宝存金融"
    description: str = "历史数据为主，仅 A 股"
    priority: int = int(os.getenv("BAOSTOCK_PRIORITY", "70"))
    _bs = None
    _login = False

    @classmethod
    def _get_bs(cls):
        """仅用于 is_available() 检查，数据查询请用 _with_connection"""
        if cls._bs is None:
            try:
                import baostock as bs
                cls._bs = bs
            except ImportError:
                cls.enabled = False
                raise
        return cls._bs

    @classmethod
    def _with_connection(cls, fn, *args, **kwargs):
        """用 login/logout 包裹每次数据查询，防止 socket 泄漏"""
        bs = cls._get_bs()
        try:
            lg = bs.login()
            if lg.error_code != '0':
                raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
            cls._login = True
            return fn(bs, *args, **kwargs)
        finally:
            try:
                bs.logout()
            except Exception:
                pass
            cls._login = False

    @classmethod
    def is_available(cls) -> bool:
        try:
            cls._get_bs()
            return cls.enabled
        except:
            return False

    @classmethod
    def _convert_symbol(cls, symbol):
        s = symbol.strip()
        return f'sh.{s}' if s.startswith('6') else f'sz.{s}'

    @classmethod
    def _query_to_df(cls, rs):
        """通用: 把 baostock ResultSet 转成 DataFrame"""
        if rs is None:
            return pd.DataFrame()
        if rs.error_code != '0':
            return pd.DataFrame()
        data = []
        try:
            while (rs.error_code == '0') & rs.next():
                data.append(rs.get_row_data())
        except:
            pass
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data, columns=rs.fields)

    @classmethod
    def _fetch_industry_data(cls, bs):
        rs = bs.query_stock_industry()
        return cls._query_to_df(rs)

    @classmethod
    def _get_industry_data(cls):
        cache = get_baostock_cache()
        try:
            cached_df = cache.get_industry_data()
            if cached_df is not None and not cached_df.empty:
                logger.debug("Baostock: 使用缓存的行业数据")
                return cached_df
        except Exception as e:
            logger.warning(f"Baostock: 读取缓存失败: {e}")
        logger.info("Baostock: 从 API 获取行业数据（可能需要60-90秒）")
        try:
            df = cls._with_connection(cls._fetch_industry_data)
            if not df.empty:
                try:
                    cache.set_industry_data(df)
                    logger.info(f"Baostock: 行业数据已缓存，共 {len(df)} 条")
                except Exception as e:
                    logger.warning(f"Baostock: 写入缓存失败: {e}")
            return df
        except Exception as e:
            logger.error(f"Baostock: 获取行业数据失败: {e}")
            return pd.DataFrame()

    # ── K线 ──────────────────────────────────────────────

    @classmethod
    def _fetch_stock_hist(cls, bs, symbol, period="daily", start="", end=""):
        bs_sym = cls._convert_symbol(symbol)
        if not end:
            end = datetime.now().strftime('%Y-%m-%d')
        else:
            try:
                if len(end) == 8 and end.isdigit():
                    end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
            except:
                pass
        if not start:
            start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        else:
            try:
                if len(start) == 8 and start.isdigit():
                    start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
            except:
                pass
        freq = {'daily': 'd', 'weekly': 'w', 'monthly': 'm'}.get(period, 'd')
        rs = bs.query_history_k_data_plus(
            bs_sym, "date,open,high,low,close,volume,amount,pctChg",
            start_date=start, end_date=end, frequency=freq, adjustflag="3"
        )
        if rs is None:
            raise RuntimeError("baostock: query_history_k_data_plus 返回 None")
        if rs.error_code != '0':
            raise RuntimeError(f"baostock: {rs.error_msg}")
        df = cls._query_to_df(rs)
        if df.empty:
            return df
        col_map = {'code': '代码', 'date': '日期', 'open': '开盘', 'high': '最高', 'low': '最低',
                   'close': '收盘', 'volume': '成交量', 'amount': '成交额', 'pctChg': '涨跌幅'}
        df = df.rename(columns=col_map)
        numeric_cols = ['开盘', '最高', '最低', '收盘', '成交量', '成交额', '涨跌幅']
        for c in numeric_cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        return df

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        try:
            return cls._with_connection(cls._fetch_stock_hist, symbol, period, start, end)
        except Exception as e:
            logger.warning(f"Baostock get_stock_hist 失败: {e}")
            raise

    # ── 实时行情：确实不支持 ──────────────────────────────

    @classmethod
    def get_spot_em(cls):
        raise NotImplementedError("baostock 不支持实时行情")

    # ── 行业板块 ─────────────────────────────────────────

    # 常见行业名映射（东财/通俗名 → baostock证监会行业关键词）
    _INDUSTRY_KEYWORDS = {
        '银行': '货币金融',
        '证券': '资本市场',
        '保险': '保险',
        '房地产': '房地产业',
        '白酒': '酒',
        '医药': '医药',
        '汽车': '汽车制造',
        '电力': '电力',
        '石油': '石油',
        '钢铁': '黑色金属',
        '有色金属': '有色金属',
        '煤炭': '煤炭',
        '半导体': '计算机、通信',
        '电子': '计算机、通信',
        '食品': '食品制造',
        '家电': '电气机械',
        '航空': '航空运输',
        '建筑': '土木工程',
        '化工': '化学',
        '纺织': '纺织',
        '传媒': '广播',
        '通信': '电信',
        '互联网': '互联网',
    }

    @classmethod
    def get_board_industry_list(cls):
        """baostock 行业分类（返回行业列表）"""
        df = cls._get_industry_data()
        if df.empty:
            return df
        if 'industry' in df.columns:
            industries = df[df['industry'].str.strip().astype(bool)][['industry']].drop_duplicates().reset_index(drop=True)
            industries.columns = ['板块名称']
            industries = industries.sort_values('板块名称').reset_index(drop=True)
            return industries
        return pd.DataFrame()

    @classmethod
    def get_board_industry_cons(cls, symbol):
        """按行业名查成分股（模糊匹配 + 通俗名映射）"""
        df = cls._get_industry_data()
        if df.empty or 'industry' not in df.columns:
            return pd.DataFrame()

        # 先查映射，再模糊匹配
        keyword = cls._INDUSTRY_KEYWORDS.get(symbol, symbol)
        matched = df[
            df['industry'].str.contains(keyword, na=False) |
            df['code_name'].str.contains(keyword, na=False)
        ]
        if matched.empty:
            return pd.DataFrame()
        result = matched[['code', 'code_name']].copy()
        result['code'] = result['code'].str.split('.').str[-1]
        result.columns = ['代码', '名称']
        return result.reset_index(drop=True)

    # ── 概念板块：baostock 没有 ─────────────────────────

    @classmethod
    def get_board_concept_cons(cls, symbol):
        raise NotImplementedError("baostock 不支持概念板块")

    @classmethod
    def get_board_concept_list(cls):
        raise NotImplementedError("baostock 不支持概念板块")

    # ── 新闻 / 评级 ──────────────────────────────────────

    @classmethod
    def get_stock_news(cls, symbol):
        raise NotImplementedError("baostock 不支持新闻")

    @classmethod
    def get_stock_rating(cls, symbol):
        raise NotImplementedError("baostock 不支持机构评级")

    # ── 财务数据 ─────────────────────────────────────────

    @classmethod
    def _fetch_financial_abstract(cls, bs, symbol):
        bs_sym = cls._convert_symbol(symbol)
        y = datetime.now().year - 1
        frames = []
        for query_fn, label in [
            (bs.query_profit_data, "盈利能力"),
            (bs.query_growth_data, "成长能力"),
            (bs.query_balance_data, "偿债能力"),
            (bs.query_operation_data, "运营能力"),
        ]:
            try:
                rs = query_fn(code=bs_sym, year=y, quarter=4)
                df = cls._query_to_df(rs)
                if not df.empty:
                    frames.append(df)
            except Exception as e:
                logger.warning(f"baostock {label}查询失败: {e}")
        if not frames:
            return pd.DataFrame()
        if len(frames) == 1:
            return frames[0]
        from functools import reduce
        common_cols = set(frames[0].columns)
        for f in frames[1:]:
            common_cols &= set(f.columns)
        merge_keys = [c for c in ['code', 'year', 'quarter'] if c in common_cols]
        if not merge_keys:
            merge_keys = list(common_cols)
        try:
            merged = reduce(lambda l, r: pd.merge(l, r, on=merge_keys, how='outer'), frames)
            return merged
        except Exception as e:
            logger.warning(f"baostock 财务数据合并失败: {e}")
            return frames[0]

    @classmethod
    def get_financial_abstract(cls, symbol):
        try:
            return cls._with_connection(cls._fetch_financial_abstract, symbol)
        except Exception as e:
            logger.warning(f"Baostock get_financial_abstract 失败: {e}")
            return pd.DataFrame()

    # ── 估值指标 ─────────────────────────────────────────

    @classmethod
    def _fetch_valuation_indicators(cls, bs, symbol):
        bs_sym = cls._convert_symbol(symbol)
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')
        rs = bs.query_history_k_data_plus(
            bs_sym, "date,peTTM,pbMRQ,psTTM,pcfNcfTTM",
            start_date=start, end_date=end, frequency="d", adjustflag="3"
        )
        if rs is None or rs.error_code != '0':
            raise RuntimeError(f"baostock 估值查询失败: {getattr(rs, 'error_msg', 'None')}")
        df = cls._query_to_df(rs)
        if df.empty:
            raise RuntimeError(f"baostock 未获取到 {symbol} 的估值数据")
        # peTTM/pbMRQ/psTTM → valuation.py 可识别的列名
        col_map = {'peTTM': '市盈率-动态', 'pbMRQ': '市净率', 'psTTM': '市销率'}
        df = df.rename(columns=col_map)
        for c in ('市盈率-动态', '市净率', '市销率', 'pcfNcfTTM'):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        # 取最近一个有效交易日（peTTM 非空）
        valid = df[df['市盈率-动态'].notna()] if '市盈率-动态' in df.columns else df
        return (valid.tail(1) if not valid.empty else df.tail(1)).reset_index(drop=True)

    @classmethod
    def get_valuation_indicators(cls, symbol):
        """个股估值指标（PE-TTM/PB/PS），baostock K线接口自带估值字段。"""
        return cls._with_connection(cls._fetch_valuation_indicators, symbol)

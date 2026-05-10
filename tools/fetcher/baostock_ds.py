"""Baostock 数据源"""
import logging
import pandas as pd
from datetime import datetime, timedelta
from .base import DataSource
from .cache import get_baostock_cache

logger = logging.getLogger("radar.fetcher")


class BaostockDataSource(DataSource):
    name: str = "baostock"
    priority: int = 85
    _bs = None
    _login = False

    @classmethod
    def _get_bs(cls):
        if cls._bs is None:
            try:
                import baostock as bs
                cls._bs = bs
            except ImportError:
                cls.enabled = False
                raise
        if not cls._login:
            lg = cls._bs.login()
            if lg.error_code != '0':
                cls.enabled = False
                raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
            cls._login = True
        return cls._bs

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
    def _get_industry_data(cls):
        """
        获取行业数据（优先缓存）
        因为 baostock 的 query_stock_industry() 特别慢，单独缓存
        """
        cache = get_baostock_cache()
        
        # 先尝试从缓存获取
        try:
            cached_df = cache.get_industry_data()
            if cached_df is not None and not cached_df.empty:
                logger.debug("Baostock: 使用缓存的行业数据")
                return cached_df
        except Exception as e:
            logger.warning(f"Baostock: 读取缓存失败: {e}")
        
        # 缓存不存在或过期，从 API 获取
        logger.info("Baostock: 从 API 获取行业数据（可能需要60-90秒）")
        try:
            bs = cls._get_bs()
            rs = bs.query_stock_industry()
            df = cls._query_to_df(rs)
            
            # 缓存结果
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
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        bs = cls._get_bs()
        bs_sym = cls._convert_symbol(symbol)
        
        # 确保日期格式正确（YYYY-MM-DD）
        if not end:
            end = datetime.now().strftime('%Y-%m-%d')
        else:
            # 尝试标准化日期格式
            try:
                if len(end) == 8 and end.isdigit():  # YYYYMMDD
                    end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
            except:
                pass
        
        if not start:
            start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        else:
            # 尝试标准化日期格式
            try:
                if len(start) == 8 and start.isdigit():  # YYYYMMDD
                    start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
            except:
                pass
        
        freq = {'daily': 'd', 'weekly': 'w', 'monthly': 'm'}.get(period, 'd')
        
        try:
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
            # 除了日期列，其他列都转成数值
            numeric_cols = ['开盘', '最高', '最低', '收盘', '成交量', '成交额', '涨跌幅']
            for c in numeric_cols:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
            return df
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
    def get_financial_abstract(cls, symbol):
        bs = cls._get_bs()
        bs_sym = cls._convert_symbol(symbol)
        y = datetime.now().year - 1

        frames = []

        # 盈利能力
        try:
            rs = bs.query_profit_data(code=bs_sym, year=y, quarter=4)
            df = cls._query_to_df(rs)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning(f"baostock 盈利能力查询失败: {e}")

        # 成长能力
        try:
            rs = bs.query_growth_data(code=bs_sym, year=y, quarter=4)
            df = cls._query_to_df(rs)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning(f"baostock 成长能力查询失败: {e}")

        # 偿债能力
        try:
            rs = bs.query_balance_data(code=bs_sym, year=y, quarter=4)
            df = cls._query_to_df(rs)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning(f"baostock 偿债能力查询失败: {e}")

        # 运营能力
        try:
            rs = bs.query_operation_data(code=bs_sym, year=y, quarter=4)
            df = cls._query_to_df(rs)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning(f"baostock 运营能力查询失败: {e}")

        if not frames:
            return pd.DataFrame()

        if len(frames) == 1:
            return frames[0]

        # 横向拼接（按共同列合并）
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
            # 回退：直接拼第一个
            return frames[0]
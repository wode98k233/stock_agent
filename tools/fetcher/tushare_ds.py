"""Tushare Pro 数据源（需token）"""
import os
import logging
import pandas as pd
from datetime import datetime, timedelta
from .base import DataSource
from .config import Config

logger = logging.getLogger("radar.fetcher")


class TushareDataSource(DataSource):
    """
    免费注册: https://tushare.pro/register
    积分>=120即可使用核心接口
    配置 TUSHARE_TOKEN 环境变量 或 Config.TUSHARE_TOKEN
    """
    name: str = "tushare"
    priority: int = 80
    _pro = None

    @classmethod
    def _get_token(cls):
        return Config.TUSHARE_TOKEN or os.environ.get('TUSHARE_TOKEN', '')

    @classmethod
    def _get_pro(cls):
        if cls._pro is None:
            token = cls._get_token()
            if not token:
                cls.enabled = False
                raise RuntimeError("Tushare token 未配置")
            import tushare as ts
            ts.set_token(token)
            cls._pro = ts.pro_api()
        return cls._pro

    @classmethod
    def is_available(cls) -> bool:
        if not cls._get_token():
            return False
        try:
            cls._get_pro()
            return cls.enabled
        except:
            return False

    @classmethod
    def _ts_code(cls, symbol):
        s = symbol.strip()
        return f'{s}.SH' if s.startswith('6') else f'{s}.SZ'

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        pro = cls._get_pro()
        ts_code = cls._ts_code(symbol)
        if not start: start = (datetime.now() - timedelta(days=120)).strftime('%Y%m%d')
        if not end: end = datetime.now().strftime('%Y%m%d')
        s, e = start.replace('-', ''), end.replace('-', '')

        if period == "daily":
            df = pro.daily(ts_code=ts_code, start_date=s, end_date=e)
        elif period == "weekly":
            df = pro.weekly(ts_code=ts_code, start_date=s, end_date=e)
        elif period == "monthly":
            df = pro.monthly(ts_code=ts_code, start_date=s, end_date=e)
        else:
            df = pro.daily(ts_code=ts_code, start_date=s, end_date=e)

        if df is None or df.empty: return pd.DataFrame()
        col_map = {'trade_date': '日期', 'open': '开盘', 'high': '最高',
                   'low': '最低', 'close': '收盘', 'vol': '成交量',
                   'amount': '成交额', 'pct_chg': '涨跌幅'}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        for c in ['开盘', '最高', '最低', '收盘', '成交量', '成交额', '涨跌幅']:
            if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
        return df

    @classmethod
    def get_spot_em(cls):
        pro = cls._get_pro()
        today = datetime.now().strftime('%Y%m%d')
        df = pro.daily(trade_date=today)
        if df is None or df.empty:
            df = pro.daily(trade_date=(datetime.now() - timedelta(days=1)).strftime('%Y%m%d'))
        if df is None or df.empty: return pd.DataFrame()
        col_map = {'ts_code': '代码', 'close': '最新价', 'open': '开盘',
                   'high': '最高', 'low': '最低', 'pre_close': '昨收',
                   'vol': '成交量', 'amount': '成交额', 'pct_chg': '涨跌幅', 'change': '涨跌额'}
        return df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    @classmethod
    def get_board_industry_cons(cls, symbol):
        pro = cls._get_pro()
        try:
            df = pro.index_member(index_code=symbol)
            if df is not None and not df.empty:
                return df.rename(columns={k: v for k, v in {'con_code': '代码', 'con_name': '名称'}.items() if k in df.columns})
        except: pass
        return pd.DataFrame()

    @classmethod
    def get_board_concept_cons(cls, symbol):
        raise NotImplementedError("Tushare 不支持概念板块成分股")

    @classmethod
    def get_board_industry_list(cls):
        pro = cls._get_pro()
        try:
            df = pro.index_basic(market='SW')
            if df is not None and not df.empty:
                return df.rename(columns={k: v for k, v in {'ts_code': '板块代码', 'name': '板块名称'}.items() if k in df.columns})
        except: pass
        return pd.DataFrame()

    @classmethod
    def get_board_concept_list(cls):
        pro = cls._get_pro()
        try:
            df = pro.index_basic(market='TH')
            if df is not None and not df.empty:
                return df.rename(columns={k: v for k, v in {'ts_code': '板块代码', 'name': '板块名称'}.items() if k in df.columns})
        except: pass
        return pd.DataFrame()

    @classmethod
    def get_stock_news(cls, symbol):
        pro = cls._get_pro()
        try:
            df = pro.news(ts_code=cls._ts_code(symbol))
            if df is not None and not df.empty: return df
        except: pass
        return pd.DataFrame()

    @classmethod
    def get_stock_rating(cls, symbol):
        pro = cls._get_pro()
        try:
            df = pro.rating(ts_code=cls._ts_code(symbol))
            if df is not None and not df.empty: return df
        except: pass
        return pd.DataFrame()

    @classmethod
    def get_financial_abstract(cls, symbol):
        pro = cls._get_pro()
        try:
            df = pro.fina_indicator(ts_code=cls._ts_code(symbol),
                                     fields='ts_code,ann_date,roe_dt,netprofit_yoy,eps')
            if df is not None and not df.empty: return df.head(8)
        except: pass
        return pd.DataFrame()
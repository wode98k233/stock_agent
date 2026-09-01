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
    label: str = "Tushare"
    description: str = "专业数据接口，支持 A/HK"
    priority: int = int(os.getenv("TUSHARE_PRIORITY", "60"))
    _pro = None

    @classmethod
    def _get_token(cls):
        return Config.TUSHARE_TOKEN

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

    # ── 估值（daily_basic 原生列 pe/pe_ttm/pb/ps_ttm/dv_ratio 已被 valuation.py 识别）──

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """个股估值指标（PE/PB/PS/股息率/总市值），取最近一个交易日。"""
        pro = cls._get_pro()
        s = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
        e = datetime.now().strftime('%Y%m%d')
        df = pro.daily_basic(
            ts_code=cls._ts_code(symbol), start_date=s, end_date=e,
            fields='ts_code,trade_date,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_mv,circ_mv'
        )
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到 {symbol} 的估值数据")
        return df.head(1)  # daily_basic 按 trade_date 降序，head(1) 即最新

    @classmethod
    def get_valuation_history(cls, symbol: str, years: int = 5) -> pd.DataFrame:
        """个股历史估值（PE/PB 时间序列），daily_basic 原生列名与 akshare 一致。"""
        pro = cls._get_pro()
        s = (datetime.now() - timedelta(days=365 * years)).strftime('%Y%m%d')
        e = datetime.now().strftime('%Y%m%d')
        df = pro.daily_basic(
            ts_code=cls._ts_code(symbol), start_date=s, end_date=e,
            fields='trade_date,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_mv'
        )
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到 {symbol} 的历史估值数据")
        return df

    # ── 资金流向 ──

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        """个股资金流向（moneyflow，含主力/大单/中单/小单净额）。仅 A 股。"""
        pro = cls._get_pro()
        s = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        e = datetime.now().strftime('%Y%m%d')
        df = pro.moneyflow(ts_code=cls._ts_code(symbol), start_date=s, end_date=e)
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到 {symbol} 的资金流向数据")
        return df

    # ── 融资融券 ──

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """融资融券汇总（按交易所）。"""
        pro = cls._get_pro()
        exchange = "SSE" if market == "sh" else "SZSE"
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')
        df = pro.margin(exchange_id=exchange,
                        start_date=start_date.replace('-', ''),
                        end_date=end_date.replace('-', ''))
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到融资融券汇总数据({market})")
        return df

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        """融资融券个股明细（按日期）。"""
        pro = cls._get_pro()
        trade_date = date.replace('-', '') if date else datetime.now().strftime('%Y%m%d')
        df = pro.margin_detail(trade_date=trade_date)
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到融资融券明细数据({trade_date})")
        return df

    # ── 大宗交易 ──

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """大宗交易明细。symbol 为 6 位个股代码时按个股查询，否则按日期查询全市场。"""
        pro = cls._get_pro()
        s = start_date.replace('-', '') if start_date else (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        e = end_date.replace('-', '') if end_date else datetime.now().strftime('%Y%m%d')
        sym = symbol.strip()
        if sym[:6].isdigit() and len(sym) >= 6:
            df = pro.block_trade(ts_code=cls._ts_code(sym), start_date=s, end_date=e)
        else:
            df = pro.block_trade(trade_date=e)
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到大宗交易数据({symbol})")
        return df

    # ── 涨停池 ──

    @classmethod
    def get_limit_up_pool(cls, date: str = None, n: int = 20) -> list:
        """涨停池（limit_list_d, limit_type='U'），返回与 akshare 一致的 dict 列表。"""
        pro = cls._get_pro()
        trade_date = (date or datetime.now().strftime('%Y%m%d')).replace('-', '')
        df = pro.limit_list_d(trade_date=trade_date, limit_type='U')
        if df is None or df.empty:
            raise RuntimeError(f"tushare 未获取到涨停池数据({trade_date})")
        if 'limit_times' in df.columns:
            df = df.sort_values('limit_times', ascending=False)
        rows = []
        for _, r in df.head(n).iterrows():
            rows.append({
                'code': str(r.get('ts_code', '')).split('.')[0],
                'name': str(r.get('name', '')).strip(),
                'change_pct': float(r.get('pct_chg', 0) or 0),
                'price': float(r.get('close', 0) or 0),
                'amount': float(r.get('amount', 0) or 0),
                'seal_amount': float(r.get('fd_amount', 0) or 0),
                'consecutive_boards': int(r.get('limit_times', 0) or 0),
                'industry': str(r.get('industry', '')).strip(),
            })
        return rows

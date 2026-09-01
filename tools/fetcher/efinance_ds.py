"""efinance 数据源（轻量级东方财富）"""
import logging
import os
import pandas as pd
from .base import DataSource, DataSourceManager

logger = logging.getLogger("radar.fetcher")


class EfinanceDataSource(DataSource):
    name: str = "efinance"
    label: str = "Efinance"
    description: str = "东方财富接口，支持 A/ETF"
    priority: int = int(os.getenv("EFINANCE_PRIORITY", "80"))
    _ef = None

    @classmethod
    def _get_ef(cls):
        if cls._ef is None:
            import efinance as ef
            cls._ef = ef
        return cls._ef

    @classmethod
    def is_available(cls) -> bool:
        try:
            cls._get_ef()
            return cls.enabled
        except:
            return False

    # ── K线 ──────────────────────────────────────────────

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        ef = cls._get_ef()
        beg = start.replace('-', '') if start else '19000101'
        _end = end.replace('-', '') if end else '20500101'
        klt = {'daily': 101, 'weekly': 102, 'monthly': 103}.get(period, 101)

        market = DataSourceManager.detect_market(symbol)
        if market == "etf":
            # ETF 用 stock API（不是 fund API），返回完整 OHLCV
            df = ef.stock.get_quote_history(symbol, beg=beg, end=_end, klt=klt, fqt=1)
        else:
            df = ef.stock.get_quote_history(symbol, beg=beg, end=_end, klt=klt)

        if df is not None and not df.empty:
            col_map = {'日期': '日期', '开盘': '开盘', '最高': '最高',
                       '最低': '最低', '收盘': '收盘', '成交量': '成交量',
                       '成交额': '成交额', '涨跌幅': '涨跌幅'}
            return df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        return pd.DataFrame()

    # ── 实时行情 ─────────────────────────────────────────

    @classmethod
    def get_spot_em(cls):
        ef = cls._get_ef()
        try:
            df = ef.stock.get_realtime_quotes()
            if df is not None and not df.empty:
                return df
        except:
            pass
        return pd.DataFrame()

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        ef = cls._get_ef()
        market = DataSourceManager.detect_market(symbol)

        if market == "etf":
            try:
                df = ef.stock.get_realtime_quotes(['ETF'])
                if df is not None and not df.empty:
                    for code_col in ('代码', '股票代码', 'code'):
                        if code_col in df.columns:
                            return df[df[code_col].astype(str) == str(symbol)].copy()
            except Exception as e:
                logger.debug(f"efinance ETF 实时失败: {e}")
            return pd.DataFrame()

        # A 股：走默认的全市场行情
        df = cls.get_spot_em()
        if df is None or df.empty:
            return pd.DataFrame()
        for code_col in ('代码', '股票代码', 'code', 'symbol'):
            if code_col in df.columns:
                return df[df[code_col].astype(str) == str(symbol)].copy()
        return pd.DataFrame()

    # ── 板块：efinance 的 get_belong_board 可以查个股所属板块 ──

    @classmethod
    def get_board_industry_cons(cls, symbol):
        """
        efinance 不支持板块成分股查询。

        efinance.stock.get_members() 仅支持指数成分股（如 000300 沪深300），
        不支持行业/概念板块成分股（BK 代码）。get_belong_board() 反向查询
        个股所属板块，也无法列出板块内所有成分股。
        """
        raise NotImplementedError("efinance 不支持板块成分股查询（get_members 仅支持指数）")

    @classmethod
    def get_board_concept_cons(cls, symbol):
        """efinance 不支持板块成分股查询"""
        raise NotImplementedError("efinance 不支持板块成分股查询（get_members 仅支持指数）")

    @classmethod
    def get_board_industry_list(cls):
        raise NotImplementedError("efinance 不支持行业板块列表查询")

    @classmethod
    def get_board_concept_list(cls):
        raise NotImplementedError("efinance 不支持概念板块列表查询")

    # ── 新闻 / 评级 / 财务：efinance 无对应接口 ──────────

    @classmethod
    def get_stock_news(cls, symbol):
        raise NotImplementedError("efinance 不支持新闻查询")

    @classmethod
    def get_stock_rating(cls, symbol):
        raise NotImplementedError("efinance 不支持机构评级")

    @classmethod
    def get_financial_abstract(cls, symbol):
        raise NotImplementedError("efinance 不支持财务摘要")

    # ── 资金流向 / 估值：efinance 原生支持（仅 A 股 / ETF）──────

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        """个股历史资金流向（主力/大单/中单/小单净流入）。

        efinance.stock.get_history_bill 返回列已是中文（主力净流入/超大单净流入/
        收盘价/涨跌幅等），与 akshare 个股资金流向格式兼容，直接返回。
        """
        market = DataSourceManager.detect_market(symbol)
        if market in ("hk", "us"):
            raise NotImplementedError(f"efinance 不支持{market}市场的资金流向")
        ef = cls._get_ef()
        df = ef.stock.get_history_bill(symbol)
        if df is None or df.empty:
            raise RuntimeError(f"efinance 未获取到 {symbol} 的资金流向数据")
        return df

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """个股估值指标（PE/PB/总市值）。

        efinance.stock.get_base_info 返回 Series：股票代码/股票名称/市盈率(动)/
        市净率/总市值/流通市值/所处行业/ROE 等，映射为 akshare 兼容列名。
        """
        market = DataSourceManager.detect_market(symbol)
        if market in ("hk", "us"):
            raise NotImplementedError(f"efinance 不支持{market}市场的估值指标")
        ef = cls._get_ef()
        s = ef.stock.get_base_info(symbol)
        if s is None or s.empty:
            raise RuntimeError(f"efinance 未获取到 {symbol} 的估值数据")
        col_map = {'市盈率(动)': '市盈率-动态', '市净率': '市净率', '总市值': '总市值'}
        row = {'股票代码': symbol}
        for src, dst in col_map.items():
            if src in s.index:
                row[dst] = pd.to_numeric(s[src], errors='coerce')
        return pd.DataFrame([row])

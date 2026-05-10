"""efinance 数据源（轻量级东方财富）"""
import logging
import pandas as pd
from .base import DataSource

logger = logging.getLogger("radar.fetcher")


class EfinanceDataSource(DataSource):
    name: str = "efinance"
    priority: int = 90
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

    # ── 板块：efinance 的 get_belong_board 可以查个股所属板块 ──

    @classmethod
    def get_board_industry_cons(cls, symbol):
        """
        efinance 不直接支持按行业板块名查成分股。
        但如果传入的是板块代码(BK开头)，尝试 get_members。
        否则抛出 NotImplementedError。
        """
        ef = cls._get_ef()
        if symbol.startswith('BK'):
            try:
                df = ef.stock.get_members(symbol)
                if df is not None and not df.empty:
                    return df.rename(columns={
                        '股票代码': '代码', '股票名称': '名称',
                        '股票权重': '权重'
                    })
            except Exception as e:
                logger.warning(f"efinance get_members({symbol}) 失败: {e}")
        raise NotImplementedError("efinance 不支持按行业名称查成分股，请使用板块代码(BK开头)")

    @classmethod
    def get_board_concept_cons(cls, symbol):
        """同行业板块，efinance 概念板块也走 get_members"""
        return cls.get_board_industry_cons(symbol)

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
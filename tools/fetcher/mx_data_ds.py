"""
妙想金融数据 (MX Data) 数据源
基于东方财富妙想 API，提供高质量金融数据
"""
import logging
import pandas as pd
import json
import os
import sys
from typing import Optional
from .base import DataSource

logger = logging.getLogger("radar.fetcher")


class MXDataDataSource(DataSource):
    name: str = "mx_data"
    priority: int = 98  # 优先级略低于 akshare，高于 sina
    _mx_data_module = None

    # mx_data 列名 → akshare 列名映射（防御性：只映射已知变体，未知列原样保留）
    _COLUMN_MAP = {
        'get_stock_hist': {
            '开盘价': '开盘', '今开': '开盘',
            '收盘价': '收盘', '最新价': '收盘',
            '最高价': '最高', '最低价': '最低',
        },
        'get_board_industry_cons': {'股票代码': '代码', '股票名称': '名称'},
        'get_board_concept_cons': {'股票代码': '代码', '股票名称': '名称'},
        'get_stock_news': {
            '标题': '新闻标题', '内容': '新闻内容',
            '时间': '发布时间', '来源': '文章来源',
        },
        'get_stock_rating': {'评级': '机构评级', '平均目标价': '目标价'},
    }

    @staticmethod
    def _normalize_columns(df: pd.DataFrame, method_name: str) -> pd.DataFrame:
        """将 mx_data 返回的列名标准化为 akshare 兼容格式"""
        mapping = MXDataDataSource._COLUMN_MAP.get(method_name, {})
        if not mapping:
            return df
        rename = {k: v for k, v in mapping.items() if k in df.columns}
        return df.rename(columns=rename) if rename else df

    @classmethod
    def _load_mx_data(cls):
        """加载 mx_data 模块"""
        if cls._mx_data_module is not None:
            return cls._mx_data_module

        # 添加 mx-data 路径
        try:
            import tools.other_skills.eastmoney.mx_data.mx_data as mx_data
            cls._mx_data_module = mx_data
            logger.debug("mx_data 模块加载成功")
            return cls._mx_data_module
        except Exception as e:
            logger.warning(f"mx_data 模块加载失败: {e}")
            cls.enabled = False
            return None

    @classmethod
    def is_available(cls) -> bool:
        """检查是否可用"""
        if not cls.enabled:
            return False

        # 检查 API Key
        if not os.getenv("MX_APIKEY"):
            logger.debug("MX_APIKEY 未配置，禁用 mx_data")
            cls.enabled = False
            return False

        # 尝试加载模块
        if not cls._load_mx_data():
            return False

        return True

    @classmethod
    def _query_mx(cls, query: str) -> pd.DataFrame:
        """通过 mx_data 自然语言查询数据，返回 DataFrame"""
        mx_data = cls._load_mx_data()
        if not mx_data:
            raise RuntimeError("mx_data 模块未加载")
        try:
            client = mx_data.MXData()
            result = client.query(query)
            tables, _, _, err = mx_data.MXData.parse_result(result)
            if err or not tables:
                raise RuntimeError(f"mx_data 查询失败: {err}，查询: {query}")
            first = tables[0]
            return pd.DataFrame(first["rows"], columns=first["fieldnames"])
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"mx_data 查询异常: {e}")

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """通过妙想API获取历史K线"""
        period_map = {"daily": "日", "weekly": "周", "monthly": "月"}
        period_cn = period_map.get(period, "日")
        date_clause = ""
        if start and end:
            s = f"{start[:4]}-{start[4:6]}-{start[6:]}" if len(start) >= 8 else start
            e = f"{end[:4]}-{end[4:6]}-{end[6:]}" if len(end) >= 8 else end
            date_clause = f" {s}至{e}"
        elif start:
            s = f"{start[:4]}-{start[4:6]}-{start[6:]}" if len(start) >= 8 else start
            date_clause = f" {s}至今"
        query = f"股票代码{symbol} {period_cn}K线{date_clause} 开盘 收盘 最高 最低 成交量 成交额 振幅 涨跌幅 涨跌额 换手率"
        df = cls._query_mx(query)
        if 'date' in df.columns:
            df = df.rename(columns={'date': '日期'})
        return cls._normalize_columns(df, 'get_stock_hist')

    @classmethod
    def get_spot_em(cls) -> pd.DataFrame:
        """mx_data 不支持全市场实时行情批量查询"""
        raise RuntimeError("mx_data 不支持全市场实时行情批量查询，请使用个股查询接口")

    @classmethod
    def get_board_industry_cons(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取行业板块成分股"""
        df = cls._query_mx(f"{symbol}板块成分股 代码 名称 最新价 涨跌幅")
        return cls._normalize_columns(df, 'get_board_industry_cons')

    @classmethod
    def get_board_concept_cons(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取概念板块成分股"""
        df = cls._query_mx(f"{symbol}概念板块成分股 代码 名称 最新价 涨跌幅")
        return cls._normalize_columns(df, 'get_board_concept_cons')

    @classmethod
    def get_board_industry_list(cls) -> pd.DataFrame:
        """通过妙想API获取行业板块列表"""
        return cls._query_mx("A股行业板块列表 板块名称 板块代码")

    @classmethod
    def get_board_concept_list(cls) -> pd.DataFrame:
        """通过妙想API获取概念板块列表"""
        return cls._query_mx("A股概念板块列表 板块名称 板块代码")

    @classmethod
    def get_stock_news(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取个股新闻"""
        df = cls._query_mx(f"股票代码{symbol} 最新新闻 新闻标题 发布时间")
        return cls._normalize_columns(df, 'get_stock_news')

    @classmethod
    def get_stock_rating(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取机构评级"""
        df = cls._query_mx(f"股票代码{symbol} 机构评级 目标价 投资评级")
        return cls._normalize_columns(df, 'get_stock_rating')

    @classmethod
    def get_financial_abstract(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取核心财务指标"""
        return cls._query_mx(f"股票代码{symbol} 核心财务指标 净利润 营业收入 ROE 毛利率 资产负债率")

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取个股估值指标"""
        return cls._query_mx(f"股票代码{symbol} 市盈率 市净率 市销率 PEG 股息率")

    @classmethod
    def get_valuation_history(cls, symbol: str, years: int = 5) -> pd.DataFrame:
        """通过妙想API获取历史估值数据"""
        return cls._query_mx(f"股票代码{symbol} 近{years}年 市盈率 市净率")

    @classmethod
    def get_industry_valuation(cls, industry_name: str) -> pd.DataFrame:
        """通过妙想API获取行业估值统计"""
        return cls._query_mx(f"{industry_name}板块 平均市盈率 平均市净率")

    # ── 资金流向 ──

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取个股资金流向"""
        return cls._query_mx(f"股票代码{symbol} 主力资金净流入 散户资金净流入 资金流向")

    @classmethod
    def get_sector_fund_flow_rank(cls, indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
        """通过妙想API获取板块资金流向排名"""
        sector_map = {"行业资金流": "行业板块", "概念资金流": "概念板块"}
        board = sector_map.get(sector_type, "行业板块")
        return cls._query_mx(f"{indicator}{board}资金流向排名 主力净流入")

    @classmethod
    def get_north_fund_flow(cls, symbol: str = "北向资金") -> pd.DataFrame:
        """通过妙想API获取北向资金数据"""
        return cls._query_mx(f"北向资金{symbol}净买入 持仓变动")

    # ── 融资融券 ──

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """通过妙想API获取融资融券汇总"""
        exchange = "上交所" if market == "sh" else "深交所"
        return cls._query_mx(f"{exchange}融资融券余额 融资买入 融券卖出")

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        """通过妙想API获取融资融券明细"""
        exchange = "上交所" if market == "sh" else "深交所"
        return cls._query_mx(f"{exchange}融资融券明细 个股")

    # ── 大宗交易 ──

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """通过妙想API获取大宗交易数据"""
        return cls._query_mx(f"大宗交易{symbol} 溢价率 成交金额 买方卖方")

    @classmethod
    def get_block_trade_stats(cls, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """通过妙想API获取大宗交易统计"""
        return cls._query_mx("大宗交易每日统计 成交金额 溢价率")

    # ── 板块轮动 ──

    @classmethod
    def get_board_industry_spot(cls, symbol: str = "行业板块") -> pd.DataFrame:
        """通过妙想API获取板块实时行情"""
        return cls._query_mx(f"{symbol}实时行情排名 涨跌幅 成交额")

    @classmethod
    def get_board_industry_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """通过妙想API获取板块历史行情"""
        return cls._query_mx(f"{symbol}板块 近期历史行情 涨跌幅")

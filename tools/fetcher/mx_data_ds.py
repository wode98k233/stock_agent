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
    label: str = "妙想"
    description: str = "东方财富 AI 接口，支持 A/HK/US 市场"
    priority: int = int(os.getenv("MX_PRIORITY", "110"))
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
        'get_stock_realtime': {
            '股票代码': '代码',
            '股票名称': '名称',
            '股票简称': '名称',
            '开盘价': '今开',
            '开盘': '今开',
            '最高价': '最高',
            '最低价': '最低',
            '昨收盘': '昨收',
            '前收盘价': '昨收',
            '收盘价': '最新价',
            '市盈率': '市盈率-动态',
            '市盈率(TTM)': '市盈率-动态',
            '市净率PB': '市净率',
        },
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
        df, _, _ = cls._query_mx_with_meta(query)
        return df

    @classmethod
    def _query_mx_with_meta(cls, query: str):
        """查询并返回 DataFrame + 原始响应 + 首张表元信息。"""
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
            df = pd.DataFrame(first["rows"], columns=first["fieldnames"])
            return df, result, first
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"mx_data 查询异常: {e}")

    @classmethod
    def _query_xuangu(cls, query: str) -> pd.DataFrame:
        """通过妙想选股API(stock-screen)查询，返回 DataFrame。

        与 _query_mx 不同，此接口能正确返回板块成分股列表。
        """
        import requests
        api_key = os.getenv("MX_APIKEY")
        if not api_key:
            return pd.DataFrame()
        try:
            url = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
            headers = {"Content-Type": "application/json", "apikey": api_key}
            resp = requests.post(url, headers=headers, json={"keyword": query}, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            status = result.get("status")
            if status != 0:
                logger.warning(f"mx_xuangu 状态码错误: {status}")
                return pd.DataFrame()

            data = result.get("data", {}).get("data", {})
            all_results = data.get("allResults", {}).get("result", {})
            data_list = all_results.get("dataList", [])
            columns = all_results.get("columns", [])

            if not data_list:
                return pd.DataFrame()

            # 构建列名映射: 原始字段名 → 中文名
            col_map = {}
            for col in columns or []:
                en_key = col.get("field", "") or col.get("name", "") or col.get("key", "")
                cn_name = col.get("displayName", "") or col.get("title", "") or col.get("label", "")
                if en_key and cn_name:
                    col_map[str(en_key)] = str(cn_name)

            # 转换为 DataFrame
            rows = []
            for row in data_list:
                if not isinstance(row, dict):
                    continue
                cn_row = {}
                for en_key, val in row.items():
                    cn_name = col_map.get(en_key, en_key)
                    if isinstance(val, (dict, list)):
                        cn_row[cn_name] = json.dumps(val, ensure_ascii=False)
                    else:
                        cn_row[cn_name] = str(val) if val is not None else ""
                rows.append(cn_row)

            return pd.DataFrame(rows) if rows else pd.DataFrame()

        except Exception as e:
            logger.warning(f"mx_xuangu 查询失败: {e}")
            return pd.DataFrame()

    @staticmethod
    def _extract_entity_name(raw_result) -> str:
        """从 MX 原始响应中提取证券名称。"""
        try:
            search = raw_result.get("data", {}).get("data", {}).get("searchDataResultDTO", {})
            dto_list = search.get("dataTableDTOList", []) or []
            for dto in dto_list:
                entity = dto.get("entityTagDTO") or {}
                for key in ("fullName", "shortName"):
                    value = entity.get(key)
                    if value:
                        return str(value)
                for key in ("entityName", "title"):
                    value = dto.get(key)
                    if value:
                        text = str(value)
                        if "(" in text:
                            return text.split("(")[0].strip()
                        return text
        except Exception:
            return ""
        return ""

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
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        """通过妙想API获取单只股票实时行情"""
        query = (
            f"股票代码{symbol} 实时行情 最新价 股票简称 开盘价 最高价 最低价 昨收盘 "
            "成交量 成交额 涨跌幅 涨跌额 换手率 振幅 量比 市盈率(TTM) 市净率 总市值"
        )
        df, raw_result, _ = cls._query_mx_with_meta(query)
        if '股票代码' not in df.columns and '代码' not in df.columns:
            df.insert(0, '代码', symbol)
        df = cls._normalize_columns(df, 'get_stock_realtime')
        if '名称' not in df.columns:
            entity_name = cls._extract_entity_name(raw_result)
            if entity_name:
                df.insert(1, '名称', entity_name)
        return df

    @classmethod
    def get_board_industry_cons(cls, symbol: str) -> pd.DataFrame:
        """通过妙想选股API获取行业板块成分股。

        使用 stock-screen 接口（而非 query 接口），能正确返回个股列表。
        """
        df = cls._query_xuangu(f"{symbol}板块成分股")
        if df is not None and not df.empty:
            return cls._normalize_columns(df, 'get_board_industry_cons')
        # fallback: 用 query 接口
        df = cls._query_mx(f"{symbol}板块成分股完整名单 包含哪些股票 股票代码 股票名称")
        return cls._normalize_columns(df, 'get_board_industry_cons')

    @classmethod
    def get_board_concept_cons(cls, symbol: str) -> pd.DataFrame:
        """通过妙想选股API获取概念板块成分股。"""
        df = cls._query_xuangu(f"{symbol}概念板块成分股")
        if df is not None and not df.empty:
            return cls._normalize_columns(df, 'get_board_concept_cons')
        df = cls._query_mx(f"{symbol}概念板块成分股完整名单 包含哪些股票 股票代码 股票名称")
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
    def get_market_news(cls, query: str = "今日A股市场热点新闻", limit: int = 50) -> pd.DataFrame:
        """通过妙想资讯搜索获取市场热点新闻"""
        try:
            from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
            mx = MXSearch()
            result = mx.search(query)
            # 解析响应
            data = result.get("data", {})
            inner = data.get("data", {})
            search_resp = inner.get("llmSearchResponse", {})
            items = search_resp.get("data", [])
            if not items:
                return pd.DataFrame()
            rows = []
            for item in items:
                if item.get("informationType") == "NOTICE":
                    continue
                rows.append({
                    "title": item.get("title", ""),
                    "summary": item.get("content", ""),
                    "source": item.get("insName", "妙想搜索"),
                    "publish_time": item.get("date", ""),
                    "url": "",
                })
            df = pd.DataFrame(rows)
            # 按时间倒序
            if "publish_time" in df.columns:
                df = df.sort_values("publish_time", ascending=False)
            if limit and len(df) > limit:
                df = df.head(limit)
            return df
        except Exception as e:
            logger.warning(f"[mx_data] 妙想资讯搜索失败: {e}")
            raise NotImplementedError(f"妙想资讯搜索不可用: {e}")

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
        return cls._query_mx(f"{indicator}{board}资金流向排名 主力净流入 涨跌幅 成交额")

    @classmethod
    def get_north_fund_flow(cls, symbol: str = "北向资金") -> pd.DataFrame:
        """通过妙想API获取北向资金数据"""
        return cls._query_mx(f"北向资金{symbol}净买入 持仓变动")

    # ── 融资融券 ──

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """通过妙想API获取融资融券汇总"""
        exchange = "上交所" if market == "sh" else "深交所"
        query = f"{exchange}融资融券余额 融资买入 融券卖出"
        if start_date and end_date:
            query = f"{query} 近{start_date}至{end_date}"
        return cls._query_mx(query)

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        """通过妙想API获取融资融券明细"""
        exchange = "上交所" if market == "sh" else "深交所"
        query = f"{exchange}融资融券明细 个股"
        if date:
            query = f"{query} {date}"
        return cls._query_mx(query)

    # ── 大宗交易 ──

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """通过妙想API获取大宗交易数据"""
        query = f"大宗交易{symbol} 溢价率 成交金额 买方卖方"
        if start_date and end_date:
            query = f"{query} {start_date}至{end_date}"
        return cls._query_mx(query)

    @classmethod
    def get_block_trade_stats(cls, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """通过妙想API获取大宗交易统计"""
        query = "大宗交易每日统计 成交金额 溢价率"
        if start_date and end_date:
            query = f"{query} {start_date}至{end_date}"
        return cls._query_mx(query)

    # ── 板块轮动 ──

    @classmethod
    def get_board_industry_spot(cls, symbol: str = "行业板块") -> pd.DataFrame:
        """通过妙想API获取板块实时行情"""
        return cls._query_mx(f"{symbol}实时行情排名 涨跌幅 成交额 换手率")

    @classmethod
    def get_board_industry_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """通过妙想API获取板块历史行情"""
        board = symbol if str(symbol).endswith("板块") else f"{symbol}板块"
        query = f"{board} 近期历史行情 涨跌幅 成交额"
        if start and end:
            query = f"{query} {start}至{end}"
        return cls._query_mx(query)

    # ── 板块排名 / 热点 / 涨停 / 筹码 ──
    # 妙想返回自然语言查询表，列名不固定；按关键词模糊定位列，
    # 字段缺失时抛 RuntimeError，让 retry 回退到 akshare（akshare 为权威源）。

    @staticmethod
    def _find_col(df: pd.DataFrame, fragments) -> Optional[str]:
        """返回第一个列名包含任一关键词的列；找不到返回 None。"""
        for col in df.columns:
            name = str(col)
            if any(frag in name for frag in fragments):
                return col
        return None

    @classmethod
    def _rankings_from_query(cls, query: str, name_frags, n: int) -> tuple:
        df = cls._query_mx(query)
        name_col = cls._find_col(df, name_frags)
        chg_col = cls._find_col(df, ['涨跌幅'])
        if not name_col or not chg_col:
            raise RuntimeError(f"mx_data 排名结果缺少名称/涨跌幅列: {list(df.columns)}")
        d = df.copy()
        d[chg_col] = pd.to_numeric(d[chg_col], errors='coerce')
        d = d.dropna(subset=[chg_col])
        if d.empty:
            raise RuntimeError("mx_data 排名结果涨跌幅全为空")
        top = [{'name': str(r[name_col]), 'change_pct': float(r[chg_col])}
               for _, r in d.nlargest(n, chg_col).iterrows()]
        bottom = [{'name': str(r[name_col]), 'change_pct': float(r[chg_col])}
                  for _, r in d.nsmallest(n, chg_col).iterrows()]
        return (top, bottom)

    @classmethod
    def get_sector_rankings(cls, n: int = 5) -> tuple:
        """行业板块涨跌排名 (涨幅前n, 跌幅前n)。"""
        return cls._rankings_from_query("今日行业板块涨跌幅排名 板块名称 涨跌幅 成交额",
                                        ['板块', '名称', '行业'], n)

    @classmethod
    def get_concept_rankings(cls, n: int = 5) -> tuple:
        """概念板块涨跌排名 (涨幅前n, 跌幅前n)。"""
        return cls._rankings_from_query("今日概念板块涨跌幅排名 板块名称 涨跌幅 成交额",
                                        ['板块', '名称', '概念'], n)

    @classmethod
    def get_hot_stocks(cls, n: int = 10) -> list:
        """热门股票人气榜。"""
        df = cls._query_mx("今日热门股票人气排行榜 股票代码 股票名称 最新价 涨跌幅")
        code_col = cls._find_col(df, ['代码'])
        name_col = cls._find_col(df, ['名称', '简称'])
        chg_col = cls._find_col(df, ['涨跌幅'])
        if not name_col or not chg_col:
            raise RuntimeError(f"mx_data 热门股票结果字段缺失: {list(df.columns)}")
        price_col = cls._find_col(df, ['最新价', '现价', '收盘'])
        rows = []
        for i, (_, r) in enumerate(df.head(n).iterrows()):
            rows.append({
                'rank': i + 1,
                'code': str(r[code_col]).strip() if code_col else '',
                'name': str(r[name_col]).strip(),
                'price': float(pd.to_numeric(r[price_col], errors='coerce') or 0) if price_col else 0.0,
                'change_pct': float(pd.to_numeric(r[chg_col], errors='coerce') or 0),
                'source': '妙想人气榜',
            })
        return rows

    @classmethod
    def get_limit_up_pool(cls, date: str = None, n: int = 20) -> list:
        """涨停池。"""
        date_clause = f" {date}" if date else "今日"
        df = cls._query_mx(f"{date_clause}涨停板股票 股票代码 股票名称 涨跌幅 最新价 成交额 换手率 连板数")
        code_col = cls._find_col(df, ['代码'])
        name_col = cls._find_col(df, ['名称', '简称'])
        if not code_col or not name_col:
            raise RuntimeError(f"mx_data 涨停池结果字段缺失: {list(df.columns)}")
        chg_col = cls._find_col(df, ['涨跌幅'])
        price_col = cls._find_col(df, ['最新价', '现价', '收盘'])
        amount_col = cls._find_col(df, ['成交额'])
        turnover_col = cls._find_col(df, ['换手率'])
        boards_col = cls._find_col(df, ['连板', '连板数'])

        def _num(r, col):
            return float(pd.to_numeric(r[col], errors='coerce') or 0) if col else 0.0

        rows = []
        for _, r in df.head(n).iterrows():
            rows.append({
                'code': str(r[code_col]).strip(),
                'name': str(r[name_col]).strip(),
                'change_pct': _num(r, chg_col),
                'price': _num(r, price_col),
                'amount': _num(r, amount_col),
                'turnover_rate': _num(r, turnover_col),
                'consecutive_boards': int(_num(r, boards_col)),
            })
        return rows

    @classmethod
    def get_chip_distribution(cls, symbol: str) -> dict:
        """筹码分布（获利比例/平均成本/集中度）。"""
        df = cls._query_mx(f"股票代码{symbol} 筹码分布 获利比例 平均成本 90成本区间 90集中度")
        profit_col = cls._find_col(df, ['获利比例', '获利盘'])
        cost_col = cls._find_col(df, ['平均成本'])
        if not profit_col and not cost_col:
            raise RuntimeError(f"mx_data 筹码分布结果字段缺失: {list(df.columns)}")
        r = df.iloc[-1]

        def _num(col):
            return float(pd.to_numeric(r[col], errors='coerce') or 0) if col else 0.0

        date_col = cls._find_col(df, ['日期', '时间'])
        return {
            'code': symbol,
            'date': str(r[date_col]) if date_col else '',
            'profit_ratio': _num(profit_col),
            'avg_cost': _num(cost_col),
            'concentration_90': _num(cls._find_col(df, ['90集中度', '集中度'])),
        }

"""YFinance 数据源 — 港美股 + A 股 ETF 兜底

priority=45（低于腾讯的 50，作为港美股兜底源）
"""
import os
import pandas as pd
from .base import DataSource, DataSourceManager


def _is_etf_code(symbol: str) -> bool:
    s = symbol.strip()
    if len(s) >= 6:
        s = s[:6]
    return (
        (s.startswith(("51", "52", "56", "58")) and len(s) == 6)
        or (s.startswith(("15", "16", "18")) and len(s) == 6)
    )


def _convert_stock_code(symbol: str) -> str:
    """转换为 Yahoo Finance ticker 格式"""
    s = symbol.strip()
    # 去除可能的后缀
    if "." in s:
        s = s.split(".")[0]

    if not s.isdigit():
        # 美股：原样返回
        return s

    if len(s) == 5:
        # 港股：5 位数字 → .HK
        return f"{s}.HK"

    if len(s) == 6:
        # A 股 / ETF
        if s.startswith(("6", "9", "5")):
            return f"{s}.SS"
        return f"{s}.SZ"

    return s


class YFinanceDataSource(DataSource):
    name = "yfinance"
    label: str = "Yahoo"
    description: str = "雅虎财经，支持全球市场"
    priority = int(os.getenv("YFINANCE_PRIORITY", "45"))

    @classmethod
    def is_available(cls) -> bool:
        try:
            import yfinance
            return cls.enabled
        except ImportError:
            return False

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        ticker = _convert_stock_code(symbol)

        def _fetch():
            t = yf.Ticker(ticker)
            yf_period = {"daily": "1d", "weekly": "1wk", "monthly": "1mo"}.get(period, "1d")
            kwargs = {}
            if start:
                kwargs["start"] = start
            if end:
                kwargs["end"] = end
            if not kwargs:
                kwargs["period"] = "6mo"
            df = t.history(**kwargs, interval=yf_period)
            return df

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_fetch)
                df = future.result(timeout=30)
        except (FuturesTimeout, Exception) as e:
            raise RuntimeError(f"YFinance 获取 {ticker} 失败: {e}")

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.reset_index()
        # 标准化列名
        col_map = {
            "Date": "日期", "Open": "开盘", "Close": "收盘",
            "High": "最高", "Low": "最低", "Volume": "成交量",
        }
        df = df.rename(columns=col_map)
        for col in ["日期", "开盘", "收盘", "最高", "最低", "成交量"]:
            if col not in df.columns:
                df[col] = None
        return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]].copy()

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        ticker = _convert_stock_code(symbol)

        def _fetch():
            t = yf.Ticker(ticker)
            info = t.info
            return info

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_fetch)
                info = future.result(timeout=30)
        except (FuturesTimeout, Exception):
            return pd.DataFrame()

        if not info:
            return pd.DataFrame()

        row = {
            "代码": symbol,
            "名称": info.get("shortName", ""),
            "最新价": info.get("currentPrice") or info.get("regularMarketPrice"),
            "涨跌幅": info.get("regularMarketChangePercent"),
            "成交量": info.get("volume"),
            "成交额": info.get("regularMarketVolume"),
            "今开": info.get("open") or info.get("regularMarketOpen"),
            "最高": info.get("dayHigh") or info.get("regularMarketDayHigh"),
            "最低": info.get("dayLow") or info.get("regularMarketDayLow"),
            "昨收": info.get("previousClose") or info.get("regularMarketPreviousClose"),
            "总市值": info.get("marketCap"),
            "市盈率": info.get("trailingPE"),
        }
        return pd.DataFrame([row])

    @classmethod
    def _get_info(cls, symbol: str) -> dict:
        """获取 Ticker.info（带超时），失败返回空 dict。"""
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        ticker = _convert_stock_code(symbol)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: yf.Ticker(ticker).info).result(timeout=30) or {}
        except (FuturesTimeout, Exception):
            return {}

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """估值指标（PE/PB/PS/股息率/总市值），列名与 valuation.py 兼容。"""
        info = cls._get_info(symbol)
        if not info:
            raise RuntimeError(f"YFinance 未获取到 {symbol} 的估值数据")
        row = {
            '股票代码': symbol,
            '市盈率-动态': info.get('trailingPE'),
            '市净率': info.get('priceToBook'),
            '市销率': info.get('priceToSalesTrailing12Months'),
            '股息率': (info.get('dividendYield') or 0),
            '总市值': info.get('marketCap'),
        }
        if not any(row.get(k) for k in ('市盈率-动态', '市净率', '市销率')):
            raise RuntimeError(f"YFinance {symbol} 估值字段缺失")
        return pd.DataFrame([row])

    @classmethod
    def get_financial_abstract(cls, symbol: str) -> pd.DataFrame:
        """财务摘要（ROE/净利率/营收增长/EPS）。"""
        info = cls._get_info(symbol)
        if not info:
            raise RuntimeError(f"YFinance 未获取到 {symbol} 的财务数据")
        row = {
            '股票代码': symbol,
            'ROE': info.get('returnOnEquity'),
            '净利率': info.get('profitMargins'),
            '营收增长': info.get('revenueGrowth'),
            'EPS': info.get('trailingEps'),
            '每股净资产': info.get('bookValue'),
        }
        if not any(v is not None for k, v in row.items() if k != '股票代码'):
            raise RuntimeError(f"YFinance {symbol} 财务字段缺失")
        return pd.DataFrame([row])

    @classmethod
    def get_stock_news(cls, symbol: str) -> pd.DataFrame:
        """个股新闻（Ticker.news），兼容新旧两种返回结构。"""
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        ticker = _convert_stock_code(symbol)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                items = pool.submit(lambda: yf.Ticker(ticker).news).result(timeout=30)
        except (FuturesTimeout, Exception) as e:
            raise RuntimeError(f"YFinance 获取 {symbol} 新闻失败: {e}")
        if not items:
            raise RuntimeError(f"YFinance 未获取到 {symbol} 的新闻")
        rows = []
        for item in items[:20]:
            # 新版结构 item['content'] 嵌套；旧版扁平
            c = item.get('content', item)
            title = c.get('title') or item.get('title', '')
            if not title:
                continue
            provider = c.get('provider') or {}
            rows.append({
                '新闻标题': title,
                '新闻内容': c.get('summary', '') or c.get('description', ''),
                '发布时间': c.get('pubDate') or item.get('providerPublishTime', ''),
                '文章来源': (provider.get('displayName') if isinstance(provider, dict) else '') or item.get('publisher', ''),
                '新闻链接': (c.get('canonicalUrl') or {}).get('url', '') if isinstance(c.get('canonicalUrl'), dict) else item.get('link', ''),
            })
        if not rows:
            raise RuntimeError(f"YFinance {symbol} 新闻解析为空")
        return pd.DataFrame(rows)


# 注册数据源
DataSourceManager.register_source(YFinanceDataSource)

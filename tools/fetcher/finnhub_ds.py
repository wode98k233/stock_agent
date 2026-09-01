"""Finnhub 数据源 — 美股专用

priority=48（低于腾讯的 50，高于 YFinance 的 45，美股专用兜底源）
API: https://finnhub.io/docs/api
限流: 60 次/分（免费 tier）
"""
import logging
import os
import re
import time

import pandas as pd
import requests

from .base import DataSource, DataSourceManager

logger = logging.getLogger("radar.fetcher.finnhub")

# 美股代码正则：1-5 个大写字母，可选 .X 后缀（如 BRK.B）
_US_STOCK_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _is_us_stock(symbol: str) -> bool:
    return bool(_US_STOCK_RE.match(symbol.strip().upper()))


class FinnhubDataSource(DataSource):
    name = "finnhub"
    label: str = "Finnhub"
    description: str = "美股数据接口"
    priority = int(os.getenv("FINNHUB_PRIORITY", "48"))

    @classmethod
    def is_available(cls) -> bool:
        from config import Config
        return bool(Config.FINNHUB_API_KEY) and cls.enabled

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        from config import Config
        api_key = Config.FINNHUB_API_KEY
        if not api_key:
            raise RuntimeError("Finnhub API key 未配置")

        sym = symbol.strip().upper()
        if not _is_us_stock(sym):
            raise RuntimeError(f"Finnhub 仅支持美股，不支持 {symbol}")

        # 转换日期为 unix timestamp
        import datetime
        if start:
            start_ts = int(datetime.datetime.strptime(start, "%Y%m%d").timestamp())
        else:
            start_ts = int((datetime.datetime.now() - datetime.timedelta(days=365)).timestamp())
        if end:
            end_ts = int(datetime.datetime.strptime(end, "%Y%m%d").timestamp())
        else:
            end_ts = int(datetime.datetime.now().timestamp())

        cls._enforce_rate_limit(0.3, 0.8)

        url = "https://finnhub.io/api/v1/stock/candle"
        params = {
            "symbol": sym,
            "resolution": "D",
            "from": start_ts,
            "to": end_ts,
            "token": api_key,
        }

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Finnhub HTTP 请求失败 {symbol}: {e}")

        if data.get("s") != "ok" or not data.get("c"):
            raise RuntimeError(f"Finnhub 无数据返回 {symbol}")

        df = pd.DataFrame({
            "日期": pd.to_datetime(data["t"], unit="s").strftime("%Y-%m-%d"),
            "开盘": data["o"],
            "收盘": data["c"],
            "最高": data["h"],
            "最低": data["l"],
            "成交量": data["v"],
        })
        return df

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        from config import Config
        api_key = Config.FINNHUB_API_KEY
        if not api_key:
            return pd.DataFrame()

        sym = symbol.strip().upper()
        if not _is_us_stock(sym):
            return pd.DataFrame()

        cls._enforce_rate_limit(0.3, 0.8)

        url = "https://finnhub.io/api/v1/quote"
        params = {"symbol": sym, "token": api_key}

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[Finnhub] 实时行情失败 {symbol}: {e}")
            return pd.DataFrame()

        price = data.get("c")
        if not price:
            return pd.DataFrame()

        row = {
            "代码": symbol,
            "名称": "",
            "最新价": price,
            "涨跌幅": data.get("dp"),
            "成交量": data.get("v"),
            "今开": data.get("o"),
            "最高": data.get("h"),
            "最低": data.get("l"),
            "昨收": data.get("pc"),
        }
        return pd.DataFrame([row])

    @classmethod
    def get_stock_news(cls, symbol: str) -> pd.DataFrame:
        from config import Config
        api_key = Config.FINNHUB_API_KEY
        if not api_key:
            return pd.DataFrame()

        sym = symbol.strip().upper()
        if not _is_us_stock(sym):
            return pd.DataFrame()

        cls._enforce_rate_limit(0.3, 0.8)

        import datetime
        today = datetime.date.today()
        week_ago = today - datetime.timedelta(days=7)

        url = "https://finnhub.io/api/v1/company-news"
        params = {
            "symbol": sym,
            "from": week_ago.isoformat(),
            "to": today.isoformat(),
            "token": api_key,
        }

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            items = resp.json()
        except Exception as e:
            logger.warning(f"[Finnhub] 新闻获取失败 {symbol}: {e}")
            return pd.DataFrame()

        if not items:
            return pd.DataFrame()

        rows = []
        for item in items[:20]:
            rows.append({
                "新闻标题": item.get("headline", ""),
                "新闻内容": item.get("summary", ""),
                "发布时间": datetime.datetime.fromtimestamp(item.get("datetime", 0)).strftime("%Y-%m-%d %H:%M") if item.get("datetime") else "",
                "新闻来源": item.get("source", ""),
                "新闻链接": item.get("url", ""),
            })
        return pd.DataFrame(rows)

    @classmethod
    def _request(cls, path: str, params: dict) -> dict:
        """通用 GET 请求，校验美股代码 + 注入 token，返回 JSON。"""
        from config import Config
        api_key = Config.FINNHUB_API_KEY
        if not api_key:
            raise RuntimeError("Finnhub API key 未配置")
        sym = str(params.get("symbol", "")).strip().upper()
        if not _is_us_stock(sym):
            raise RuntimeError(f"Finnhub 仅支持美股，不支持 {params.get('symbol')}")
        cls._enforce_rate_limit(0.3, 0.8)
        params = {**params, "symbol": sym, "token": api_key}
        resp = requests.get(f"https://finnhub.io/api/v1{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """估值指标（PE/PB/PS/股息率/市值），列名与 valuation.py 兼容。"""
        data = cls._request("/stock/metric", {"symbol": symbol, "metric": "all"})
        m = data.get("metric") or {}
        if not m:
            raise RuntimeError(f"Finnhub 未获取到 {symbol} 的估值数据")
        row = {
            '股票代码': symbol,
            '市盈率-动态': m.get('peTTM'),
            '市净率': m.get('pbAnnual') or m.get('pbQuarterly'),
            '市销率': m.get('psTTM'),
            '股息率': m.get('currentDividendYieldTTM'),
            '总市值': m.get('marketCapitalization'),
        }
        if not any(row.get(k) for k in ('市盈率-动态', '市净率', '市销率')):
            raise RuntimeError(f"Finnhub {symbol} 估值字段缺失")
        return pd.DataFrame([row])

    @classmethod
    def get_financial_abstract(cls, symbol: str) -> pd.DataFrame:
        """财务摘要（ROE/净利率/EPS/营收增长）。"""
        data = cls._request("/stock/metric", {"symbol": symbol, "metric": "all"})
        m = data.get("metric") or {}
        if not m:
            raise RuntimeError(f"Finnhub 未获取到 {symbol} 的财务数据")
        row = {
            '股票代码': symbol,
            'ROE': m.get('roeTTM'),
            '净利率': m.get('netProfitMarginTTM'),
            'EPS': m.get('epsTTM') or m.get('epsBasicExclExtraItemsTTM'),
            '营收增长': m.get('revenueGrowthTTMYoy'),
            '毛利率': m.get('grossMarginTTM'),
        }
        if not any(v is not None for k, v in row.items() if k != '股票代码'):
            raise RuntimeError(f"Finnhub {symbol} 财务字段缺失")
        return pd.DataFrame([row])

    @classmethod
    def get_stock_rating(cls, symbol: str) -> pd.DataFrame:
        """机构评级（买入/持有/卖出分布趋势）。"""
        items = cls._request("/stock/recommendation", {"symbol": symbol})
        if not items:
            raise RuntimeError(f"Finnhub 未获取到 {symbol} 的评级数据")
        rows = []
        for it in items[:6]:
            rows.append({
                '评级期间': it.get('period', ''),
                '强烈买入': it.get('strongBuy', 0),
                '买入': it.get('buy', 0),
                '持有': it.get('hold', 0),
                '卖出': it.get('sell', 0),
                '强烈卖出': it.get('strongSell', 0),
            })
        return pd.DataFrame(rows)


# 注册数据源
DataSourceManager.register_source(FinnhubDataSource)

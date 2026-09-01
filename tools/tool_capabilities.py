"""工具市场能力声明与识别。

React 模式用这里的声明收敛候选工具；tool node 也用同一套规则做执行前保护。
"""
from __future__ import annotations

import re
from typing import Iterable


MARKET_ASHARE = "ashare"
MARKET_HK = "hk"
MARKET_US = "us"
MARKET_UNKNOWN = "unknown"

MARKET_LABELS = {
    MARKET_ASHARE: "A股",
    MARKET_HK: "港股",
    MARKET_US: "美股",
    MARKET_UNKNOWN: "未知市场",
}


HK_US_ALLOWED_TOOLS = {
    "mx_data_query",
    "mx_search_news",
    "calc_technical_indicators",
}


US_TICKER_WHITELIST = {
    "AAPL",
    "ADBE",
    "AMD",
    "AMZN",
    "ARM",
    "ASML",
    "AVGO",
    "BABA",
    "BIDU",
    "BEKE",
    "COIN",
    "CRM",
    "GOOG",
    "GOOGL",
    "INTC",
    "JD",
    "LI",
    "META",
    "MSFT",
    "MSTR",
    "MU",
    "NFLX",
    "NIO",
    "NTES",
    "NVDA",
    "ORCL",
    "PDD",
    "PLTR",
    "QCOM",
    "SAP",
    "SE",
    "SHOP",
    "SMCI",
    "SOFI",
    "TSLA",
    "TSM",
    "TME",
    "UBER",
    "XPEV",
}

UPPERCASE_NON_TICKERS = {
    "ADR",
    "AI",
    "ATR",
    "BB",
    "BOLL",
    "CCI",
    "CPU",
    "CPO",
    "DCF",
    "DDM",
    "DMI",
    "EPS",
    "ESG",
    "ETF",
    "FOF",
    "GPU",
    "HK",
    "IPO",
    "KDJ",
    "LOF",
    "MACD",
    "MA",
    "MA5",
    "MA10",
    "MA20",
    "MA60",
    "OBV",
    "OTC",
    "PB",
    "PCB",
    "PE",
    "PEG",
    "PS",
    "PSY",
    "QDII",
    "REIT",
    "REITS",
    "ROA",
    "ROE",
    "ROI",
    "RSI",
    "SaaS",
    "TMT",
    "US",
    "VR",
    "WR",
}


def infer_market_from_text(text: str | None) -> str:
    """从用户输入或工具参数文本中识别市场类型。"""
    raw = str(text or "")
    upper = raw.upper()

    if re.search(r"\b\d{1,5}\.(?:HK|HKG)\b", upper) or re.search(r"\bHK[:：]\s*\d{1,5}\b", upper):
        return MARKET_HK
    if re.search(r"\b[A-Z]{1,5}\.(?:US|N|O|NASDAQ|NYSE)\b", upper):
        return MARKET_US

    if re.search(r"(?<!\d)(?:600|601|603|605|688|689|000|001|002|003|300|301)\d{3}(?!\d)", raw):
        return MARKET_ASHARE
    if re.search(r"(?<!\d)\d{5}(?!\d)", raw):
        return MARKET_HK

    if any(word in raw for word in ("港股", "港交所", "香港股票", "H股")):
        return MARKET_HK
    if any(word in raw for word in ("美股", "纳斯达克", "纽交所", "NYSE", "NASDAQ", "ADR")):
        return MARKET_US
    if any(word in raw for word in ("A股", "沪深", "上证", "深证", "创业板", "科创板")):
        return MARKET_ASHARE

    for candidate in re.findall(r"\b[A-Z]{1,5}\b", upper):
        if candidate in UPPERCASE_NON_TICKERS:
            continue
        if candidate in US_TICKER_WHITELIST:
            return MARKET_US

    return MARKET_UNKNOWN


def infer_market_from_tool_args(args: dict | None, fallback_text: str | None = None) -> str:
    """优先从工具参数识别市场，参数无有效标的时回退到用户输入。"""
    args = args or {}
    for key in ("symbol", "code", "ticker", "stock_code"):
        value = args.get(key)
        if value:
            market = infer_market_from_text(str(value))
            if market != MARKET_UNKNOWN:
                return market
    for key in ("query", "symbol_or_news", "keyword", "board_name"):
        value = args.get(key)
        if value:
            market = infer_market_from_text(str(value))
            if market != MARKET_UNKNOWN:
                return market
    return infer_market_from_text(fallback_text)


def tool_supports_market(tool_name: str, market: str) -> bool:
    """判断工具是否支持当前市场。

    A 股和未知市场保持原行为；港股/美股只开放 MX 与已适配的技术指标工具。
    """
    if market in (MARKET_UNKNOWN, MARKET_ASHARE):
        return True
    if market in (MARKET_HK, MARKET_US):
        return tool_name in HK_US_ALLOWED_TOOLS
    return True


def filter_tools_for_market(tools: Iterable, market: str) -> tuple[list, list[str]]:
    """按市场过滤 LangChain 工具，返回保留工具和被过滤工具名。"""
    if market in (MARKET_UNKNOWN, MARKET_ASHARE):
        return list(tools), []

    kept = []
    filtered = []
    seen = set()
    for item in tools:
        name = getattr(item, "name", "")
        if name in seen:
            continue
        seen.add(name)
        if tool_supports_market(name, market):
            kept.append(item)
        else:
            filtered.append(name)
    return kept, filtered


def market_constraint_prompt(market: str) -> str:
    """给 skill selector 的市场约束提示。"""
    if market not in (MARKET_HK, MARKET_US):
        return ""
    label = MARKET_LABELS.get(market, "该市场")
    return (
        f"\n\n## 市场约束\n当前问题识别为{label}标的。"
        "React 快速模式下仅选择 mx_data、mx_search、technical_analysis；"
        "不要选择 A 股专用的 money_flow、valuation、stock_query、margin_trading、block_trades 等技能。"
    )


def unsupported_tool_message(tool_name: str, market: str, args: dict | None) -> str:
    """生成执行前跳过提示。"""
    args = args or {}
    label = MARKET_LABELS.get(market, "该市场")
    symbol = args.get("symbol") or args.get("code") or args.get("ticker") or ""
    symbol_text = f" {symbol}" if symbol else ""
    return (
        f"工具 {tool_name} 当前不支持{label}{symbol_text}，已跳过；"
        "请优先使用 mx_data_query / mx_search_news 获取该市场的行情、资金、估值和新闻数据。"
    )

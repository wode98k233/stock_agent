"""
选股雷达 - 简单数据查询场景处理器

用户说"茅台现在多少钱"时的执行流程：
1. 识别查询目标（股票代码）
2. 获取对应数据
3. 格式化输出（简短一句话）
"""
import logging
import re
from typing import Optional

from agents.scenarios.common import (
    resolve_stock,
    format_amount,
    format_pct,
    format_market_cap,
    fetch_stock_bundle,
)

logger = logging.getLogger("radar.scenario")


async def handle_data_query(user_input, enriched_input, context, data_timestamp, budget=None) -> Optional[str]:
    """处理简单数据查询"""
    stock_code, stock_name = resolve_stock(context)
    if not stock_code:
        return None

    query_type = _parse_query_type(enriched_input)  # 使用 enriched_input

    bundle = await fetch_stock_bundle(stock_code)
    data = bundle.get("realtime", {})

    if not data:
        return None

    return _format_answer(stock_name, stock_code, data, query_type, data_timestamp)


def _parse_query_type(user_input: str) -> str:
    """解析查询类型"""
    patterns = {
        "price": r'价格|多少钱|现价|最新价',
        "pct_chg": r'涨跌幅|涨幅|跌幅|涨跌',
        "pe": r'PE|市盈率',
        "pb": r'PB|市净率',
        "volume": r'成交量|成交额',
        "turnover": r'换手率',
        "market_cap": r'市值|总市值',
        "dividend": r'股息率',
    }
    for qtype, pattern in patterns.items():
        if re.search(pattern, user_input, re.IGNORECASE):
            return qtype
    return "all"


def _format_answer(stock_name, stock_code, data, query_type, data_timestamp) -> str:
    """格式化单行答案"""
    from output.formatter import kv_line, data_time_tag
    name = stock_name or stock_code

    def get(key, default="N/A"):
        v = data.get(key)
        return v if v is not None else default

    # 预提取常用值，避免 f-string 中使用反斜杠
    price = get("price")
    price_str = f"{price} 元"

    if query_type == "price":
        return f"{name}({stock_code})，{kv_line('现价', price_str)}，涨幅 {format_pct(get('pct_chg', 0))} {data_time_tag(data_timestamp)}"

    if query_type == "pct_chg":
        return f"{name}({stock_code})，今日涨幅 {format_pct(get('pct_chg', 0))}，{kv_line('现价', price_str)} {data_time_tag(data_timestamp)}"

    if query_type == "pe":
        pe = get("pe", 0)
        pe_str = f"{pe}" if pe and pe > 0 else "N/A"
        return f"{name}({stock_code})，{kv_line('市盈率(动态)', pe_str)}，{kv_line('现价', price_str)} {data_time_tag(data_timestamp)}"

    if query_type == "pb":
        pb = get("pb", 0)
        pb_str = f"{pb}" if pb and pb > 0 else "N/A"
        return f"{name}({stock_code})，{kv_line('市净率', pb_str)}，{kv_line('现价', price_str)} {data_time_tag(data_timestamp)}"

    if query_type == "volume":
        return f"{name}({stock_code})，{kv_line('成交额', format_amount(get('amount', 0)))}，涨幅 {format_pct(get('pct_chg', 0))} {data_time_tag(data_timestamp)}"

    if query_type == "turnover":
        turnover_rate = get("turnover_rate", 0)
        return f"{name}({stock_code})，{kv_line('换手率', f'{turnover_rate}%')}，{kv_line('现价', price_str)} {data_time_tag(data_timestamp)}"

    if query_type == "market_cap":
        return f"{name}({stock_code})，{kv_line('总市值', format_market_cap(get('total_mv', 0)))}，{kv_line('现价', price_str)} {data_time_tag(data_timestamp)}"

    if query_type == "dividend":
        return f"{name}({stock_code})，{kv_line('股息率', get('dividend_yield', 'N/A'))}，{kv_line('现价', price_str)} {data_time_tag(data_timestamp)}"

    parts = [f"{name}({stock_code})"]
    parts.append(kv_line("现价", f"{get('price')} 元"))
    parts.append(f"涨幅 {format_pct(get('pct_chg', 0))}")
    pe = get("pe", 0)
    if pe and pe > 0:
        parts.append(kv_line("PE", f"{pe}"))
    amount = get("amount", 0)
    if amount and amount > 10000:
        parts.append(kv_line("成交额", format_amount(amount)))
    parts.append(kv_line("换手率", f"{get('turnover_rate', 0)}%"))
    parts.append(data_time_tag(data_timestamp))
    return "，".join(parts)

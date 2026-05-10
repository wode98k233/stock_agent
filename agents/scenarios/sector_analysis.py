"""
选股雷达 - 板块热点分析场景处理器

用户说"CPO板块最近为什么涨得这么厉害"时的执行流程：
1. 识别板块名
2. 获取板块行情
3. 获取成分股 → 找龙头
4. 获取板块相关新闻
5. LLM分析驱动因素
6. 格式化输出
"""
import asyncio
import json
import logging
from typing import Optional
from output.formatter import render_sector_analysis
from output.time_util import format_news_time

logger = logging.getLogger("radar.scenario")


async def handle_sector_analysis(
    user_input: str,
    enriched_input: str,
    context: dict,
    data_timestamp: str,
    budget=None,
) -> Optional[str]:
    """处理板块热点分析场景"""

    # 1. 识别板块
    from agents.scenarios.common import resolve_sector
    sector_name = resolve_sector(context, user_input)
    if not sector_name:
        return None

    # 2. 获取板块成分股
    from agents.scenarios.common import get_board_stocks, get_realtime_quotes
    stock_codes = get_board_stocks(sector_name, logger)
    if not stock_codes:
        return None

    # 3. 获取行情数据
    quotes = await get_realtime_quotes(stock_codes, logger)

    # 4. 计算板块行情和龙头股
    sector_quote = await _compute_sector_quote(quotes, stock_codes)
    top_stocks = _find_top_stocks(quotes, None, top_n=5)

    # 5. 获取相关新闻
    news = await _get_sector_news(sector_name, top_stocks)

    # 6. LLM分析驱动因素（使用 enriched_input）
    driver_analysis = await _analyze_drivers(
        enriched_input, sector_name, sector_quote, top_stocks, news, budget
    )

    # 7. 格式化输出
    return render_sector_analysis(
        sector_name=sector_name,
        sector_quote=sector_quote,
        top_stocks=top_stocks,
        driver_analysis=driver_analysis,
        data_timestamp=data_timestamp,
    )


async def _compute_sector_quote(quotes: list, stock_codes: list) -> dict:
    """从成分股行情计算板块整体行情"""
    if not quotes:
        return {"pct_chg": 0, "amount": 0, "pct_chg_5d": 0}

    total_amount = sum(q.get("amount", 0) for q in quotes)
    avg_pct = sum(q.get("pct_chg", 0) for q in quotes) / len(quotes) if quotes else 0
    
    # 计算5日涨幅：并行获取前几只龙头股的历史数据计算平均
    avg_pct_5d = await _compute_5day_pct(stock_codes[:5])

    return {
        "pct_chg": round(avg_pct, 2),
        "amount": total_amount,
        "pct_chg_5d": round(avg_pct_5d, 2),
    }


async def _compute_5day_pct(stock_codes: list) -> float:
    """并行获取股票5日涨幅"""
    if not stock_codes:
        return 0.0
    
    from agents.scenarios.common import fetch_stock_bundle
    
    pcts = []
    # 并行获取历史数据
    tasks = [_fetch_history_for_5day(code) for code in stock_codes]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for pct in results:
        if isinstance(pct, float):
            pcts.append(pct)
    
    if not pcts:
        return 0.0
    return sum(pcts) / len(pcts)


async def _fetch_history_for_5day(code: str) -> Optional[float]:
    """获取单只股票5日涨幅（直接获取K线，避免浪费IO）"""
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        # 在 executor 中运行同步的 K 线获取
        from tools.stock_data import get_stock_history
        history = await loop.run_in_executor(None, get_stock_history, code, 10, logger)
        if history is not None and len(history) >= 5:
            # 计算最近5日的涨跌幅
            last_close = history.iloc[-1]["close"]
            prev_5_close = history.iloc[-5]["close"]
            if prev_5_close > 0:
                return ((last_close - prev_5_close) / prev_5_close * 100)
    except Exception as e:
        logger.debug(f"计算5日涨幅失败 {code}: {e}")
    return 0.0


def _find_top_stocks(quotes: list, stocks_df, top_n: int = 5) -> list:
    """找出涨幅前N的龙头股"""
    if not quotes:
        return []

    # 按涨幅排序
    sorted_quotes = sorted(quotes, key=lambda x: x.get("pct_chg", 0), reverse=True)

    # 构建代码到名称的映射
    name_map = {}
    if stocks_df is not None:
        code_col = next((c for c in ['代码', 'code', '股票代码'] if c in stocks_df.columns), None)
        name_col = next((c for c in ['名称', 'name', '股票名称'] if c in stocks_df.columns), None)
        if code_col and name_col:
            for _, row in stocks_df.iterrows():
                name_map[str(row[code_col])] = str(row[name_col])

    top = []
    for q in sorted_quotes[:top_n]:
        code = q.get("code", "")
        top.append({
            "code": code,
            "name": name_map.get(code, q.get("name", code)),
            "pct_chg": q.get("pct_chg", 0),
            "price": q.get("price", 0),
        })

    return top


async def _get_sector_news(sector_name: str, top_stocks: list) -> list:
    """并行获取板块相关新闻"""
    from tools.stock_data import get_stock_news

    loop = asyncio.get_running_loop()
    # 并行获取前5只股票的新闻
    tasks = [
        loop.run_in_executor(None, get_stock_news, s["code"], 3, logger)
        for s in top_stocks[:5]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_news = []
    seen_titles = set()
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            continue
        stock_name = top_stocks[i]["name"] if i < len(top_stocks) else ""
        for item in result:
            title = item.get("title", "")
            if title and title not in seen_titles:
                seen_titles.add(title)
                item["source_stock"] = stock_name
                all_news.append(item)

    return all_news[:10]


async def _analyze_drivers(
    user_input: str,
    sector_name: str,
    sector_quote: dict,
    top_stocks: list,
    news: list,
    budget=None,
) -> str:
    """LLM分析板块驱动因素"""
    from agents.prompts import SECTOR_ANALYSIS_PROMPT
    from agents.scenarios.common import BaseScenarioHandler

    news_brief = []
    for n in news[:6]:
        news_brief.append({
            "title": n.get("title", ""),
            "time": format_news_time(n.get("time", "")),
            "source": n.get("source", ""),
            "source_stock": n.get("source_stock", ""),
        })

    prompt = SECTOR_ANALYSIS_PROMPT.format(
        sector_name=sector_name,
        user_input=user_input,
        pct_chg=sector_quote.get("pct_chg", 0),
        amount=sector_quote.get("amount", 0),
        pct_chg_5d=sector_quote.get("pct_chg_5d", 0),
        top_stocks_json=json.dumps(top_stocks, ensure_ascii=False, default=str),
        news_json=json.dumps(news_brief, ensure_ascii=False, default=str),
    )
    return await BaseScenarioHandler.call_llm(prompt, logger, "sector-drivers", budget=budget)

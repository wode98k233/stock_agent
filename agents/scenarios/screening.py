"""
选股雷达 - 条件选股场景处理器

用户说"帮我找几只技术面金叉的消费股"时的执行流程：
1. LLM解析条件 → 提取板块名 + 筛选规则
2. 获取板块成分股
3. 批量获取实时行情
4. 先筛选基础条件（不需要K线）
5. 对通过的股票并行获取K线并计算技术指标
6. 格式化输出
"""
import json
import logging
import asyncio
from typing import Optional, Dict, Any
from output.formatter import render_screening_result

logger = logging.getLogger("radar.scenario")

# ── 提示词（从 agents/prompts.py 迁入）────────────────────────
SCREENING_PARSE_PROMPT = """从用户输入中提取选股条件。返回JSON：
{{
  "board_name": "板块名（如有，没有则为空字符串）",
  "conditions": [
    {{"field": "pe", "op": "<", "value": 20}},
    {{"field": "pct_chg", "op": ">", "value": 3}},
    {{"field": "macd_signal", "op": "==", "value": "golden_cross"}}
  ]
}}

字段映射：
- 市盈率/PE → field="pe"
- 市净率/PB → field="pb"
- 涨幅/涨跌幅 → field="pct_chg"
- 换手率 → field="turnover_rate"
- 价格/股价 → field="price"
- 市值/总市值 → field="total_mv"
- MACD金叉 → field="macd_signal", op="==", value="golden_cross"
- MACD死叉 → field="macd_signal", op="==", value="death_cross"
- RSI → field="rsi_14", op="<" 或 ">"
- KDJ金叉 → field="kdj_signal", op="==", value="golden_cross"
- 主力资金净流入 → field="main_fund_flow", op=">", value="0"

用户输入：{user_input}
"""


async def handle_screening(
    user_input: str,
    enriched_input: str,
    context: dict,
    data_timestamp: str,
    budget=None,
):
    """处理条件选股场景"""

    # 1. 解析筛选条件
    parsed = await _parse_conditions(enriched_input,budget)  # 使用 enriched_input
    board_name = parsed.get("board_name", "")
    conditions = parsed.get("conditions", [])

    if not board_name and not conditions:
        return None  # 无法解析条件，降级到Agent

    # 2. 获取候选股票
    from agents.scenarios.common import ScenarioResult
    candidates = _get_candidates(board_name, context)
    if not candidates:
        condition_text = "、".join(
            c.get("field", str(c)) if isinstance(c, dict) else str(c)
            for c in conditions
        ) if conditions else "未知条件"
        text = render_screening_result(
            board_name=board_name or "全市场",
            conditions=condition_text,
            stocks=[],
            data_timestamp=data_timestamp,
        )
        return ScenarioResult(text=text, data={"conditions": conditions, "board_name": board_name})

    # 3. 获取实时行情
    from agents.scenarios.common import get_realtime_quotes
    quotes = await get_realtime_quotes(candidates, logger)

    # 4. 拆分条件：基础条件 vs 技术指标/资金条件
    basic_conditions, tech_conditions, fund_conditions = _split_conditions(conditions)

    # 5. 先筛选基础条件（不需要额外IO）
    filtered = _apply_basic_filters(quotes, basic_conditions)

    # 6. 如果有技术指标条件，并行获取K线并筛选
    if tech_conditions and filtered:
        filtered = await _apply_tech_filters_parallel(filtered, tech_conditions)

    # 7. 如果有资金条件，并行获取资金数据并筛选
    if fund_conditions and filtered:
        filtered = await _apply_fund_filters_parallel(filtered, fund_conditions)

    # 8. 排序（按涨幅降序）
    filtered.sort(key=lambda x: x.get("pct_chg", 0), reverse=True)

    # 9. 格式化输出
    condition_text = "、".join(
        c.get("field", str(c)) if isinstance(c, dict) else str(c)
        for c in conditions
    ) if conditions else "默认筛选"
    text = render_screening_result(
        board_name=board_name or "全市场",
        conditions=condition_text,
        stocks=filtered,
        data_timestamp=data_timestamp,
    )
    return ScenarioResult(text=text, data={"conditions": conditions, "board_name": board_name, "filtered": filtered})


async def _parse_conditions(user_input: str, budget) -> dict:
    """用LLM解析用户的筛选条件，输出结构化JSON"""
    from agents.scenarios.common import BaseScenarioHandler

    prompt = SCREENING_PARSE_PROMPT.format(user_input=user_input)
    result = await BaseScenarioHandler.call_llm(prompt, logger, "screening-parse", budget=budget, json_mode=True)
    if result:
        return result

    from agents.scenario_router import extract_sector_names
    sectors = extract_sector_names(user_input)
    return {
        "board_name": sectors[0] if sectors else "",
        "conditions": [],
    }


def _get_candidates(board_name: str, context: dict) -> list:
    """获取候选股票列表"""
    if not board_name:
        from agents.scenarios.common import resolve_sector
        board_name = resolve_sector(context)
    if not board_name:
        return []

    try:
        from tools.stock_data import get_board_stocks
        df = get_board_stocks(board_name, logger)
        from agents.scenarios.common import extract_stock_codes
        return extract_stock_codes(df)
    except Exception as e:
        logger.warning(f"获取板块 {board_name} 成分股失败: {e}")

    return []


def _get_realtime_quotes_one_by_one(candidates: list) -> list:
    """逐个获取实时行情（降级方案）"""
    from tools.stock_data import get_stock_realtime
    results = []
    for code in candidates[:50]:  # 限制最多50只
        try:
            data = get_stock_realtime(code, logger)
            if data:
                results.append(data)
        except Exception:
            continue
    return results


def _split_conditions(conditions: list) -> tuple:
    """拆分条件为基础条件、技术指标条件和资金条件"""
    basic_fields = {"pe", "pb", "pct_chg", "turnover_rate", "price", "total_mv", "volume_ratio"}
    tech_fields = {"macd_signal", "kdj_signal", "rsi_14", "rsi_6", "ma_trend"}
    fund_fields = {"main_fund_flow", "north_fund_flow"}
    
    basic = []
    tech = []
    fund = []
    
    for cond in conditions:
        field = cond.get("field", "")
        if field in basic_fields:
            basic.append(cond)
        elif field in tech_fields:
            tech.append(cond)
        elif field in fund_fields:
            fund.append(cond)
    
    return basic, tech, fund


def _apply_basic_filters(quotes: list, conditions: list) -> list:
    """只筛选基础条件（不需要额外IO）"""
    if not conditions:
        return quotes
    return [q for q in quotes if _match_basic_conditions(q, conditions)]


def _match_basic_conditions(stock: dict, conditions: list) -> bool:
    """检查基础条件"""
    if not conditions:
        return True
    return all(_check_basic_condition(stock, c) for c in conditions)


def _check_basic_condition(stock: dict, condition: dict) -> bool:
    """检查单个基础条件"""
    field = condition.get("field", "")
    op = condition.get("op", "")
    value = condition.get("value")

    basic_fields = {
        "pe": "pe", "pb": "pb", "pct_chg": "pct_chg",
        "turnover_rate": "turnover_rate", "price": "price",
        "total_mv": "total_mv", "volume_ratio": "volume_ratio",
    }
    
    if field not in basic_fields:
        return True  # 不是基础条件，默认通过
    
    stock_val = stock.get(basic_fields[field])
    if stock_val is None:
        return False
    try:
        stock_val = float(stock_val)
        value = float(value)
    except (TypeError, ValueError):
        return False

    if op == "<":
        return 0 < stock_val < value
    elif op == ">":
        return stock_val > value
    elif op == "<=":
        return 0 < stock_val <= value
    elif op == ">=":
        return stock_val >= value
    elif op == "==":
        return abs(stock_val - value) < 0.001
    return False


async def _apply_tech_filters_parallel(quotes: list, conditions: list) -> list:
    """并行获取K线并筛选技术指标条件"""
    if not conditions or not quotes:
        return quotes
    
    # 限制并行数量，避免请求过多
    max_parallel = 10
    filtered = []
    skipped_count = 0
    
    # 分批处理
    for i in range(0, len(quotes), max_parallel):
        batch = quotes[i:i+max_parallel]
        # 并行检查每只股票
        tasks = [_check_stock_tech_conditions(stock, conditions) for stock in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for stock, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.debug(f"技术指标检查异常 {stock.get('code')}: {result}")
                skipped_count += 1
            elif result is True:
                filtered.append(stock)
            else:
                # 结果为False或None的情况，不加入筛选结果
                pass
    
    if skipped_count > 0:
        logger.debug(f"技术指标筛选跳过 {skipped_count} 只股票（数据获取失败）")
    
    return filtered


async def _check_stock_tech_conditions(stock: dict, conditions: list) -> bool:
    """检查单只股票的技术指标条件（单个股票）"""
    code = stock.get("code", "")
    if not code:
        return False
    
    try:
        # 在executor中运行同步的K线获取
        loop = asyncio.get_running_loop()
        df = await loop.run_in_executor(None, _get_stock_history_safe, code)
        
        if df is None or df.empty:
            return False
        
        from tools.tech_indicators import calc_indicators
        indicators = calc_indicators(df)
        if not indicators:
            return False
        
        return all(_check_tech_condition(indicators, cond) for cond in conditions)
    
    except Exception as e:
        logger.debug(f"技术指标检查失败 {code}: {e}")
        return False


def _get_stock_history_safe(code: str):
    """安全获取K线（用于线程池）"""
    from tools.stock_data import get_stock_history
    return get_stock_history(code, 60, logger)


def _check_tech_condition(indicators: dict, condition: dict) -> bool:
    """检查单个技术指标条件"""
    field = condition.get("field", "")
    op = condition.get("op", "")
    value = condition.get("value")

    if field == "macd_signal":
        macd_cross = indicators.get("macd_cross", "无交叉")
        if value == "golden_cross":
            return macd_cross == "金叉"
        elif value == "death_cross":
            return macd_cross == "死叉"
    elif field == "kdj_signal":
        kdj_status = indicators.get("kdj_status", "中性")
        if value == "golden_cross":
            return kdj_status == "金叉"
        elif value == "death_cross":
            return kdj_status == "死叉"
    elif field == "rsi_14" or field == "rsi_6":
        rsi = indicators.get("rsi", 50)
        if op == "<":
            return rsi < value
        elif op == ">":
            return rsi > value
    elif field == "ma_trend":
        close = indicators.get("current_price", 0)
        ma20 = indicators.get("ma20", 0)
        if value == "above":
            return close > ma20 > 0
        elif value == "below":
            return close < ma20
    return True


async def _apply_fund_filters_parallel(quotes: list, conditions: list) -> list:
    """并行获取资金数据并筛选"""
    if not conditions or not quotes:
        return quotes
    
    max_parallel = 10
    filtered = []
    skipped_count = 0
    
    for i in range(0, len(quotes), max_parallel):
        batch = quotes[i:i+max_parallel]
        tasks = [_check_stock_fund_conditions(stock, conditions) for stock in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for stock, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.debug(f"资金数据检查异常 {stock.get('code')}: {result}")
                skipped_count += 1
            elif result is True:
                filtered.append(stock)
            else:
                # 结果为False或None的情况，不加入筛选结果
                pass
    
    if skipped_count > 0:
        logger.debug(f"资金数据筛选跳过 {skipped_count} 只股票（数据获取失败）")
    
    return filtered


async def _check_stock_fund_conditions(stock: dict, conditions: list) -> bool:
    """检查单只股票的资金条件"""
    code = stock.get("code", "")
    if not code:
        return False
    
    try:
        loop = asyncio.get_running_loop()
        fund_data = await loop.run_in_executor(None, _get_fund_data_safe, code)
        
        if fund_data is None:
            return False
        
        return all(_check_fund_condition(fund_data, cond) for cond in conditions)
    
    except Exception as e:
        logger.debug(f"资金数据检查失败 {code}: {e}")
        return False


def _get_fund_data_safe(code: str) -> Optional[Dict]:
    """安全获取资金数据"""
    from tools.fetcher import ak_individual_fund_flow
    df = ak_individual_fund_flow(code)
    if df is not None and not df.empty:
        main_col = [c for c in df.columns if "主力" in str(c) and "净" in str(c)]
        if main_col:
            return {"main_fund": float(df.iloc[0][main_col[0]])}
    return None


def _check_fund_condition(fund_data: dict, condition: dict) -> bool:
    """检查单个资金条件"""
    field = condition.get("field", "")
    op = condition.get("op", "")
    value = condition.get("value")
    
    if field == "main_fund_flow":
        val = fund_data.get("main_fund", 0)
        if op == ">":
            return val > float(value)
        elif op == "<":
            return val < float(value)
    elif field == "north_fund_flow":
        return True
    return True

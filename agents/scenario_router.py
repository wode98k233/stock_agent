"""
选股雷达 - 场景路由器
意图分类 → 场景匹配 → 路由到对应处理器

设计原则：
1. 快速路径：规则匹配（零LLM消耗）覆盖80%的常见输入
2. 慢速路径：LLM分类兜底剩余模糊输入
3. 每个场景有明确的输入约束和输出格式
"""
import re
import logging
from typing import Optional, Tuple
from enum import Enum

logger = logging.getLogger("radar.scenario")


class Scenario(Enum):
    """场景枚举"""
    SCREENING = "screening"              # 条件选股
    STOCK_ANALYSIS = "stock_analysis"    # 个股深度分析
    SECTOR_ANALYSIS = "sector_analysis"  # 板块热点分析
    MARKET_OVERVIEW = "market_overview"  # 市场概览
    DATA_QUERY = "data_query"            # 简单数据查询
    COMPARISON = "comparison"            # 对比分析


# ── 股票代码/名称提取 ──────────────────────────────────────

# 常见股票名称（高频出现的，用于快速识别）
_well_known_stocks = {
    "贵州茅台": "600519", "茅台": "600519",
    "长江电力": "600900", "比亚迪": "002594",
    "中国平安": "601318", "平安银行": "000001",
    "招商银行": "600036", "五粮液": "000858",
    "宁德时代": "300750", "中芯国际": "688981",
    "腾讯": "00700", "阿里巴巴": "09988",
    "工商银行": "601398", "建设银行": "601939",
    "农业银行": "601288", "中国银行": "601988",
    "贵州茅台酒": "600519", "洋河股份": "002304",
    "泸州老窖": "000568", "山西汾酒": "600809",
    "隆基绿能": "601012", "通威股份": "600438",
    "阳光电源": "300274", "药明康德": "603259",
    "恒瑞医药": "600276", "海天味业": "603288",
    "中国中免": "601888", "紫金矿业": "601899",
    "华为": None,  # 未上市
    "寒武纪": "688256",
}

# 板块/概念名称（用于区分板块和个股）
_common_sectors = [
    "电力", "新能源", "半导体", "芯片", "光伏", "锂电", "储能",
    "白酒", "消费", "医药", "医疗", "军工", "券商", "银行",
    "房地产", "汽车", "人工智能", "AI", "算力", "CPO", "光模块",
    "机器人", "无人驾驶", "氢能", "碳中和", "数字经济", "稀土",
    "煤炭", "钢铁", "有色", "化工", "石油", "天然气", "黄金",
    "旅游", "酒店", "餐饮", "影视", "游戏", "传媒", "教育",
    "物流", "快递", "电商", "零售", "家电", "家居", "建材",
    "5G", "通信", "物联网", "云计算", "大数据", "网络安全",
    "区块链", "元宇宙", "虚拟现实", "AR", "VR",
]


def validate_stock_code(code: str) -> bool:
    """校验是否为合法A股股票代码格式"""
    if not code or not isinstance(code, str):
        return False
    if len(code) != 6 or not code.isdigit():
        return False
    return code[:2] in ("60", "68", "00", "30")


def extract_stock_codes(text: str) -> list:
    """从文本中提取合法的A股股票代码"""
    # \b 在中文和数字之间不匹配，改用 lookbehind/lookahead 确保恰好6位
    codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', text)
    valid = [c for c in codes if validate_stock_code(c)]
    return list(dict.fromkeys(valid))  # 去重保序


def extract_stock_names(text: str) -> list:
    """从文本中提取股票名称"""
    found = []
    for name in sorted(_well_known_stocks.keys(), key=len, reverse=True):
        if name in text:
            code = _well_known_stocks[name]
            if code:
                found.append({"name": name, "code": code})
    return found


def extract_sector_names(text: str) -> list:
    """从文本中提取板块/概念名称"""
    found = []
    for sector in sorted(_common_sectors, key=len, reverse=True):
        if sector in text:
            found.append(sector)
    return found


# ── 意图分类器 ─────────────────────────────────────────────

def classify_scenario(user_input: str) -> Tuple[Optional[Scenario], dict]:
    """
    分类用户意图，返回 (场景类型, 提取的上下文信息)

    Returns:
        (Scenario, {
            "stock_codes": [...],
            "stock_names": [...],
            "sector_names": [...],
            "raw_input": str
        })
    """
    text = user_input.strip()
    context = {
        "stock_codes": extract_stock_codes(text),
        "stock_names": extract_stock_names(text),
        "sector_names": extract_sector_names(text),
        "raw_input": text,
    }

    # ── 规则1: 对比分析（优先级最高，因为通常包含两只股票） ──
    comparison_keywords = r'对比|比较|哪个好|选哪个|哪个强|和.*比'
    if re.search(comparison_keywords, text):
        stock_count = len(context["stock_codes"]) + len(context["stock_names"])
        if stock_count >= 2:
            logger.info(f"[意图] 对比分析 (识别到{stock_count}只股票)")
            return Scenario.COMPARISON, context

    # ── 规则2: 条件选股 ──
    screening_patterns = [
        r'(?:找|推荐|筛选|有哪些|哪些).*(?:股票|股|个股)',
        r'(?:股票|股|个股).*(?:有哪些|推荐|筛选)',
        r'(?:市盈率|PE|PB|ROE|股息率|换手率).*(?:低于|高于|大于|小于|不超过)',
        r'(?:MACD|RSI|KDJ|金叉|死叉|放量|缩量|突破|均线)',
        r'(?:主力资金|资金流入|北向资金|融资|融券)',
        r'(?:适合|值得).*(?:买|投资|持有|短线|中线|长线)',
    ]
    for pattern in screening_patterns:
        if re.search(pattern, text):
            logger.info(f"[意图] 条件选股 (匹配: {pattern})")
            return Scenario.SCREENING, context

    # ── 规则3: 市场概览 ──
    market_patterns = [
        r'(?:大盘|市场|A股|股市).*(?:怎么样|情况|涨跌|今日|今天)',
        r'(?:今日|今天).*(?:大盘|市场|A股|股市)',
        r'(?:早报|晚报|盘前|盘后|复盘|早盘|午盘|尾盘)',
        r'(?:早盘|午盘|尾盘).*(?:总结|简报|行情|分析)',
        r'(?:市场|大盘).*(?:整体|总览|概览|简报)',
        r'今天.*(?:板块|热点).*(?:在涨|涨了|跌了)',
        r'哪些板块.*(?:在涨|涨了|上涨)',
    ]
    for pattern in market_patterns:
        if re.search(pattern, text):
            logger.info(f"[意图] 市场概览 (匹配: {pattern})")
            return Scenario.MARKET_OVERVIEW, context

    # ── 规则4: 板块分析 ──
    sector_patterns = [
        r'(?:板块|概念).*(?:为什么|怎么回事|逻辑|原因)',
        r'(?:为什么|怎么回事).*(?:板块|概念).*(?:涨|跌)',
        r'(?:为什么|怎么回事).*(?:涨|跌).*(?:这么|那么)',
        r'(?:板块|概念).*(?:还能|可以).*(?:追|买|持有)',
        r'(?:板块|概念).*(?:怎么样|前景|趋势)',
        r'分析.*(?:板块|概念)',
    ]
    for pattern in sector_patterns:
        if re.search(pattern, text):
            logger.info(f"[意图] 板块分析 (匹配: {pattern})")
            return Scenario.SECTOR_ANALYSIS, context

    # 如果有板块名但没有股票名，倾向于板块分析
    if context["sector_names"] and not context["stock_names"] and not context["stock_codes"]:
        # 但要排除简单的查询（如"电力板块有哪些股票"是选股）
        if not re.search(r'有哪些|哪些|推荐|筛选', text):
            logger.info(f"[意图] 板块分析 (识别到板块名，无股票名)")
            return Scenario.SECTOR_ANALYSIS, context

    # ── 规则5: 个股分析 ──
    stock_analysis_patterns = [
        r'(?:分析|看看|研究|评估|诊断)',
        r'(?:怎么样|值不值得|可以.*吗|适合.*吗|能.*吗)',
        r'(?:技术面|基本面|未来走势|走势|趋势)',
        r'(?:买入|卖出|加仓|减仓|持有|建仓)',
    ]
    has_stock = context["stock_codes"] or context["stock_names"]
    
    # 先定义数据查询关键词，避免后面重复
    data_query_patterns = [
        r'(?:多少钱|价格|股价|现价|最新价)',
        r'(?:涨跌幅|涨幅|跌幅|涨跌)',
        r'(?:成交量|成交额|换手率|量比)',
        r'(?:PE|PB|市盈率|市净率|股息率)',
        r'(?:市值|总市值|流通市值)',
    ]
    
    if has_stock:
        # 先检查是否有数据查询关键词，避免被个股分析截流
        has_data_query = any(re.search(p, text) for p in data_query_patterns)
        
        for pattern in stock_analysis_patterns:
            if re.search(pattern, text):
                logger.info(f"[意图] 个股分析 (识别到股票+分析关键词)")
                return Scenario.STOCK_ANALYSIS, context
        
        # 有股票代码/名称但没有明确的分析关键词，默认走个股分析
        # 但要排除"有哪些"这类选股表述，以及数据查询关键词
        if not re.search(r'有哪些|哪些|推荐|筛选', text) and not has_data_query:
            logger.info(f"[意图] 个股分析 (识别到股票名，默认)")
            return Scenario.STOCK_ANALYSIS, context

    # ── 规则6: 简单数据查询 ──
    if has_stock:
        for pattern in data_query_patterns:
            if re.search(pattern, text):
                logger.info(f"[意图] 数据查询 (匹配: {pattern})")
                return Scenario.DATA_QUERY, context

    # ── 规则7: "分析" + 板块名 → 板块分析 ──
    if context["sector_names"] and re.search(r'分析', text):
        logger.info(f"[意图] 板块分析 ('分析'+板块名)")
        return Scenario.SECTOR_ANALYSIS, context

    # ── 规则8: "分析" + 股票名 → 个股分析 ──
    if has_stock and re.search(r'分析|看看|研究|怎么样', text):
        logger.info(f"[意图] 个股分析 ('分析'+股票名)")
        return Scenario.STOCK_ANALYSIS, context

    # ── 规则9: 有板块名 + 选股关键词 → 选股 ──
    if context["sector_names"] and re.search(r'有哪些|哪些|推荐|找|筛选', text):
        logger.info(f"[意图] 条件选股 (板块名+选股词)")
        return Scenario.SCREENING, context

    # ── 兜底: 无法分类 ──
    logger.info(f"[意图] 无法分类，返回None交由Agent处理")
    return None, context


def classify_with_llm(user_input: str, llm, logger_obj) -> Optional[Scenario]:
    """
    LLM兜底分类（当规则无法匹配时使用）
    """
    from utils.llm_factory import llm_json_with_retry

    prompt = f"""你是一个股票投资助手，请对用户输入进行意图分类，选择最合适的一个场景。

可选场景：
- DATA_QUERY: 简单数据查询，如查询股价、涨跌幅、PE、市值等单一数据
- STOCK_ANALYSIS: 个股分析，如询问某只股票怎么样、分析某只股票、是否适合买入等
- SECTOR_ANALYSIS: 板块分析，如询问某个板块的情况、分析某个板块、推荐某个板块的股票等
- MARKET_OVERVIEW: 市场概览，如询问今天市场怎么样、今天大盘如何等
- SCREENING: 条件选股，如询问有哪些股票满足某些条件、推荐满足某些条件的股票等
- COMPARISON: 股票对比，如对比两只或多只股票、哪只股票更好等

用户输入：{user_input}

请只返回一个JSON，格式为：{{"scenario": "场景名称"}}，不要包含其他文字。
如果实在无法分类，返回：{{"scenario": null}}
"""

    try:
        result = llm_json_with_retry(
            llm,
            [("user", prompt)],
            logger_obj,
            label="classify-intent",
            skip_cache_prefix=True,
        )
        if result:
            scenario_name = result.get("scenario")
            if scenario_name:
                try:
                    return Scenario(scenario_name)
                except ValueError:
                    logger_obj.warning(f"[LLM分类] 无效场景名称: {scenario_name}")
    except Exception as e:
        logger_obj.warning(f"[LLM分类] 异常: {e}")
    return None

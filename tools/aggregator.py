"""
选股雷达 - 汇总分析工具
统计概览保留（纯数据统计）
评分、筛选、排序、报告解读 → 全部交给 LLM
"""
import json
import logging
from utils.llm_factory import get_llm, llm_json_with_retry


def _get_logger(logger):
    if logger is not None:
        return logger
    from utils.logger import get_child_logger
    return get_child_logger("aggregator")


# ── 字段标准化辅助函数 ─────────────────────────────────────

def _normalize_stock(ind: dict) -> dict:
    """
    统一股票数据字段命名，兼容多种来源的字段名差异。
    输入可以是 {symbol, code, stock_code} 中任意一种。
    输出统一为 {code, name, price, change_pct, ...}
    """
    return {
        'code': ind.get('code', '') or ind.get('symbol', '') or ind.get('stock_code', ''),
        'name': ind.get('name', '') or ind.get('stock_name', ''),
        'price': float(ind.get('price', 0) or ind.get('current_price', 0) or ind.get('close', 0) or 0),
        'change_pct': float(ind.get('change_pct', 0) or ind.get('change', 0) or ind.get('pct_1d', 0) or 0),
        'volume': int(ind.get('volume', 0) or ind.get('vol', 0) or 0),
        'sector': ind.get('sector', '') or ind.get('industry', ''),
        'macd_cross': ind.get('macd_cross', '') or _extract_macd_signal(ind),
        'ma_cross': ind.get('ma_cross', '') or _extract_ma_signal(ind),
        'rsi': float(ind.get('rsi', 0) or 0),
        'rsi_status': ind.get('rsi_status', '') or _derive_rsi_status(ind.get('rsi', 0)),
        'volume_ratio': float(ind.get('volume_ratio', 0) or 0),
        'volume_status': ind.get('volume_status', ''),
        'kdj_status': ind.get('kdj_status', '') or _extract_kdj_signal(ind),
        'bb_position': ind.get('bb_position', ''),
        'dif': float(ind.get('dif', 0) or _extract_nested(ind, 'macd', 'dif', 0) or 0),
        'dea': float(ind.get('dea', 0) or _extract_nested(ind, 'macd', 'dea', 0) or 0),
    }


def _extract_macd_signal(ind: dict) -> str:
    """从嵌套 macd 结构中提取信号"""
    macd = ind.get('macd', {})
    if isinstance(macd, dict):
        signal = macd.get('signal', '')
        if signal:
            return signal
        diff = float(macd.get('diff', macd.get('dif', 0)) or 0)
        dea = float(macd.get('dea', 0) or 0)
        if diff > dea:
            return '金叉'
        elif diff < dea:
            return '死叉'
    return ''


def _extract_ma_signal(ind: dict) -> str:
    """从 ma5/ma10/ma20 推断均线信号"""
    ma5 = float(ind.get('ma5', 0) or 0)
    ma10 = float(ind.get('ma10', 0) or 0)
    ma20 = float(ind.get('ma20', 0) or 0)
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            return '金叉'
        elif ma5 < ma10 < ma20:
            return '死叉'
    return ''


def _derive_rsi_status(rsi) -> str:
    """根据 RSI 值推断状态"""
    try:
        rsi = float(rsi)
        if rsi >= 80:
            return '超买'
        elif rsi <= 20:
            return '超卖'
    except (ValueError, TypeError):
        pass
    return ''


def _extract_kdj_signal(ind: dict) -> str:
    """从嵌套 kdj 结构中提取信号"""
    kdj = ind.get('kdj', {})
    if isinstance(kdj, dict):
        k = float(kdj.get('k', 0) or 0)
        d = float(kdj.get('d', 0) or 0)
        if k and d:
            return '金叉' if k > d else '死叉'
    return ''


def _extract_nested(d: dict, key1: str, key2: str, default=None):
    """安全提取嵌套字典"""
    inner = d.get(key1, {})
    if isinstance(inner, dict):
        return inner.get(key2, default)
    return default


# ── 统计概览（纯计数，合理保留）──────────────────────────────

def stocks_overview(indicators_list: list, logger=None) -> dict:
    """对一批股票的技术指标做统计计数 + 分布统计"""
    import numpy as np

    total = len(indicators_list)
    if total == 0:
        return {'total': 0, 'summary': '无股票数据'}

    normalized = [_normalize_stock(i) for i in indicators_list]

    stats = {
        'total': total,
        'macd_golden': sum(1 for i in normalized if i.get('macd_cross') == '金叉'),
        'macd_death': sum(1 for i in normalized if i.get('macd_cross') == '死叉'),
        'ma_golden': sum(1 for i in normalized if i.get('ma_cross') == '金叉'),
        'ma_death': sum(1 for i in normalized if i.get('ma_cross') == '死叉'),
        'rsi_overbought': sum(1 for i in normalized if i.get('rsi_status') == '超买'),
        'rsi_oversell': sum(1 for i in normalized if i.get('rsi_status') == '超卖'),
        'volume_surge': sum(1 for i in normalized if i.get('volume_ratio', 0) > 1.5),
        'kdj_golden': sum(1 for i in normalized if i.get('kdj_status') == '金叉'),
        'kdj_death': sum(1 for i in normalized if i.get('kdj_status') == '死叉'),
    }

    # 分布统计
    rsi_values = [i['rsi'] for i in normalized if i.get('rsi') is not None]
    vol_ratios = [i['volume_ratio'] for i in normalized if i.get('volume_ratio') is not None]
    pct_changes = [i['change_pct'] for i in normalized if i.get('change_pct') is not None]

    if rsi_values:
        stats['rsi_median'] = round(float(np.median(rsi_values)), 2)
        stats['rsi_mean'] = round(float(np.mean(rsi_values)), 2)
    if vol_ratios:
        stats['volume_ratio_mean'] = round(float(np.mean(vol_ratios)), 2)
    if pct_changes:
        stats['price_change_mean'] = round(float(np.mean(pct_changes)), 2)
        stats['rising_count'] = sum(1 for p in pct_changes if p > 0)
        stats['falling_count'] = sum(1 for p in pct_changes if p < 0)

    n = total
    rising = stats.get('rising_count', 0)
    falling = stats.get('falling_count', 0)
    rsi_med = stats.get('rsi_median', '-')
    stats['summary'] = (
        f"共{n}只 | 涨{rising}/跌{falling} | "
        f"MACD金叉{stats['macd_golden']}/死叉{stats['macd_death']} | "
        f"均线金叉{stats['ma_golden']}/死叉{stats['ma_death']} | "
        f"RSI中位{rsi_med} 超买{stats['rsi_overbought']}/超卖{stats['rsi_oversell']} | "
        f"放量{stats['volume_surge']}只"
    )
    return stats


# ── LLM 智能筛选 ───────────────────────────────────────────

FILTER_SYSTEM = """你是资深证券分析师。根据用户的筛选需求和股票数据，智能筛选。

用户给你:
1. 一批股票的技术指标数据
2. 筛选条件（自然语言）

要理解用户真实意图，综合判断，不只看单一指标。

输出严格 JSON:
{
  "selected": [
    {"code": "股票代码", "name": "股票名称", "match_reason": "选中理由"}
  ],
  "summary": "筛选结果总结"
}
"""


def llm_filter_stocks(indicators_list: list, user_condition: str, memory_mgr=None, logger=None) -> list:
    """让 LLM 智能筛选，接收自然语言条件"""
    logger = _get_logger(logger)
    llm = get_llm()
    normalized = [_normalize_stock(i) for i in indicators_list]
    slim = []
    for ind in normalized:
        slim.append({
            'code': ind.get('code', ''),
            'name': ind.get('name', ''),
            'price': ind.get('price', 0),
            'pct_1d': ind.get('change_pct', 0),
            'macd_cross': ind.get('macd_cross', ''),
            'ma_cross': ind.get('ma_cross', ''),
            'rsi': ind.get('rsi', 0),
            'rsi_status': ind.get('rsi_status', ''),
            'volume_ratio': ind.get('volume_ratio', 0),
            'volume_status': ind.get('volume_status', ''),
            'kdj_status': ind.get('kdj_status', ''),
            'bb_position': ind.get('bb_position', ''),
            'dif': ind.get('dif', 0),
            'dea': ind.get('dea', 0),
        })

    messages = [
        ("system", FILTER_SYSTEM),
        ("user", f"筛选条件: {user_condition}\n\n股票数据:\n{json.dumps(slim, ensure_ascii=False)}")
    ]

    if memory_mgr:
        memory_mgr.disable()
    try:
        result = llm_json_with_retry(llm, messages, logger, label="filter")
        if result and 'selected' in result:
            selected_codes = {s['code'] for s in result['selected']}
            reasons = {s['code']: s.get('match_reason', '') for s in result['selected']}
            filtered = []
            for ind in normalized:
                if ind.get('code', '') in selected_codes:
                    ind_copy = dict(ind)
                    ind_copy['filter_reason'] = reasons.get(ind_copy['code'], '')
                    filtered.append(ind_copy)
            return filtered
        return []
    finally:
        if memory_mgr:
            memory_mgr.enable()


# ── LLM 智能排序 ───────────────────────────────────────────

RANK_SYSTEM = """你是资深证券分析师。对股票进行智能排序。

给你:
1. 股票技术指标和情感分析数据
2. 排序意图（如"最适合买入的"、"最安全的"）

综合技术面、情感面、基本面给出排序。

输出严格 JSON:
{
  "ranked": [
    {"code": "股票代码", "rank": 1, "reason": "排在此位置的理由，50字以内"}
  ]
}
"""


def llm_rank_stocks(stock_data_list: list, sort_intent: str, top_n: int = 10, memory_mgr=None, logger=None) -> list:
    """让 LLM 智能排序，sort_intent 是自然语言"""
    logger = _get_logger(logger)
    llm = get_llm()
    normalized = [_normalize_stock(i) for i in stock_data_list]
    messages = [
        ("system", RANK_SYSTEM),
        ("user", f"排序意图: {sort_intent}\n取前{top_n}只\n\n股票数据:\n{json.dumps(normalized, ensure_ascii=False, default=str)}")
    ]

    if memory_mgr:
        memory_mgr.disable()
    try:
        result = llm_json_with_retry(llm, messages, logger, label="rank")
        if result and 'ranked' in result:
            return result['ranked'][:top_n]
        return []
    finally:
        if memory_mgr:
            memory_mgr.enable()


# ── LLM 多维度分析报告 ─────────────────────────────────────

REPORT_SYSTEM = """你是资深证券分析师，为单只股票生成全面分析报告。

输入包含: 实时行情、技术指标、新闻情感、机构评级、财务数据。

输出严格 JSON:
{
  "code": "股票代码",
  "name": "股票名称",
  "current_price": 当前价格,
  "pct_chg": 涨跌幅,

  "technical_analysis": {
    "trend": "上升/下降/震荡",
    "support": 支撑价位,
    "resistance": 阻力价位,
    "macd_signal": "MACD信号解读",
    "ma_signal": "均线信号解读",
    "volume_signal": "量价关系解读",
    "key_indicators": "关键指标亮点"
  },

  "sentiment_analysis": {
    "overall": "利好/利空/中性",
    "score": 情感分数,
    "key_news": "关键新闻影响"
  },

  "fundamental": {
    "valuation": "估值水平",
    "financial_health": "财务健康度"
  },

  "recommendation": {
    "verdict": "强烈推荐/推荐/中性/谨慎/回避",
    "target_price": 目标价,
    "stop_loss": 止损价,
    "holding_period": "短线/中线/长线",
    "reason": "推荐核心理由，200字以内"
  },

  "risks": ["风险点"],
  "opportunities": ["机会点"]
}
"""


MACRO_SYSTEM = """你是资深宏观经济分析师。根据提供的宏观数据生成分析报告。

不要编造任何具体股票数据。如果数据中没有个股信息，不要生成个股相关内容。

输出严格 JSON:
{
  "report_type": "macro",
  "title": "报告标题",
  "summary": "核心结论（200字以内）",
  "key_findings": ["发现1", "发现2", "发现3"],
  "market_impact": "对A股市场的影响分析",
  "sector_impacts": [{"sector": "板块名", "impact": "利好/利空", "reason": "原因"}],
  "risk_factors": ["风险1", "风险2"],
  "opportunities": ["机会1", "机会2"]
}
"""

_MACRO_KEYWORDS = {"汇率", "GDP", "利率", "CPI", "PPI", "央行", "货币政策", "美元", "人民币",
                   "美联储", "降息", "加息", "通胀", "失业率", "贸易", "关税", "宏观", "经济数据"}


def _is_macro_data(data: dict) -> bool:
    """检测数据是否为宏观类型（不含个股代码，或包含宏观关键词）"""
    # 有明确的股票代码 → 个股
    code = data.get("code", "") or data.get("stock_info", {}).get("code", "")
    if code and len(str(code)) >= 4:
        return False
    # 检测宏观关键词
    text = json.dumps(data, ensure_ascii=False)[:500]
    return any(kw in text for kw in _MACRO_KEYWORDS)


def _build_macro_report(data: dict, memory_mgr=None, logger=None) -> dict:
    """生成宏观分析报告"""
    logger = _get_logger(logger)
    llm = get_llm()
    messages = [
        ("system", MACRO_SYSTEM),
        ("user", f"请根据以下宏观数据生成分析报告:\n\n{json.dumps(data, ensure_ascii=False, default=str)[:2000]}")
    ]
    if memory_mgr:
        memory_mgr.disable()
    try:
        result = llm_json_with_retry(llm, messages, logger, label="macro-report")
        if result:
            return result
        return {"report_type": "macro", "title": "宏观分析", "summary": "报告生成失败", "key_findings": []}
    finally:
        if memory_mgr:
            memory_mgr.enable()


def llm_build_report(stock_data: dict, memory_mgr=None, logger=None) -> dict:
    """让 LLM 构建分析报告 — 自动检测数据类型选择报告模式"""
    if _is_macro_data(stock_data):
        return _build_macro_report(stock_data, memory_mgr, logger)

    logger = _get_logger(logger)
    llm = get_llm()
    messages = [
        ("system", REPORT_SYSTEM),
        ("user", f"请为以下股票生成分析报告:\n\n{json.dumps(stock_data, ensure_ascii=False, default=str)}")
    ]

    if memory_mgr:
        memory_mgr.disable()
    try:
        result = llm_json_with_retry(llm, messages, logger, label="report")
        if result:
            return result
        return {'code': stock_data.get('code', ''), 'name': stock_data.get('name', ''),
                'recommendation': {'verdict': '中性', 'reason': '报告生成失败'}}
    finally:
        if memory_mgr:
            memory_mgr.enable()


# ── LLM 技术指标解读 ───────────────────────────────────────

TECH_INTERPRET_SYSTEM = """你是技术分析专家。根据原始技术指标数据，生成专业、自然的技术面分析。
不要罗列数值，要解读指标之间的关系和含义。

输出严格 JSON:
{
  "trend": "趋势判断",
  "signals": ["信号1", "信号2"],
  "summary": "200字以内综合技术面分析",
  "action": "买入/持有/减仓/观望"
}
"""


def llm_tech_interpret(indicators: dict, memory_mgr=None, logger=None) -> dict:
    """让 LLM 解读技术指标"""
    logger = _get_logger(logger)
    llm = get_llm()
    messages = [
        ("system", TECH_INTERPRET_SYSTEM),
        ("user", f"请解读以下技术指标:\n\n{json.dumps(indicators, ensure_ascii=False, default=str)}")
    ]

    if memory_mgr:
        memory_mgr.disable()
    try:
        result = llm_json_with_retry(llm, messages, logger, label="tech_interpret")
        if result:
            return result
        return {'trend': '数据不足', 'signals': [], 'summary': '无法解读', 'action': '观望'}
    finally:
        if memory_mgr:
            memory_mgr.enable()

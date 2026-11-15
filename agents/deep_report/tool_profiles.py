"""长报告子代理的工具面 profile（2026-11-01 与用户对齐，方案 B）。

为什么要 profile 而不是「全部 53」或「只给能力 18」：
- 全量：持仓/策略/报告套娃这类与取数无关的工具会占用 schema，也容易误调；
- 只给 18：会误伤研究类工具（行情序列、两融、筹码、ETF、网页检索）——深度报告迟早要用。

边界原则：**按“一份研报需要什么”划分，不按“哪个技能有工具”划分**。
能力层（capabilities.py）只声明语义；本文件决定“这一档给哪些工具”。
"""
from __future__ import annotations

from typing import Dict, List

from agents.deep_report import capabilities as caps

# 研究类：不在 9 个能力里，但深度报告会用到（行业空间/事件/资金/筹码/ETF/估值模型）
RESEARCH_TOOLS: tuple[str, ...] = (
    "web_search", "web_fetch",                       # 行业空间、政策事件、研报线索
    "get_sector_history",                            # 板块指数历史序列（比个股历史更贴题）
    "get_margin_summary", "get_margin_detail",       # 两融（资金面）
    "get_chip_distribution", "get_risk_metrics",     # 筹码、风险（个股深度）
    "get_etf_history", "get_etf_spot", "search_funds",  # ETF/主题报告
    "calc_dcf_valuation", "calc_ddm_valuation",      # 估值模型（个股深度，板块不用）
)

# 主题类（黄金/铜/猪/红利/债基/恒生科技）：做主题深度时才挂
THEME_TOOLS: tuple[str, ...] = (
    "get_dividend_env", "describe_dividend_thesis",
    "get_bond_fund_env", "describe_bond_fund_thesis",
    "get_gold_env", "describe_gold_thesis",
    "get_cu_env", "describe_cu_thesis",
    "get_hog_env", "describe_hog_thesis",
    "get_hang_seng_env", "describe_hang_seng_thesis",
)

# 情绪/盘面（做“短线情绪”“板块异动”类章节时才挂）
MARKET_REVIEW_TOOLS: tuple[str, ...] = (
    "get_limit_up_pool", "get_limit_break_pool", "get_lianban_ladder",
    "get_stock_anomaly", "get_dragon_tiger_list", "get_hot_stocks",
    "get_market_breadth", "analyze_news_sentiment", "search_news_by_keyword",
)

# 明确不进报告子代理：持仓、策略研究、报告套娃
EXCLUDED: tuple[str, ...] = (
    "get_positions", "get_position_detail", "get_pnl_summary", "get_trade_history",
    "create_strategy_research", "get_strategy_research_status", "adopt_best_strategy",
    "stocks_overview", "llm_filter_stocks", "llm_rank_stocks", "llm_build_report",
    "llm_tech_interpret",
    "get_block_trade_detail", "get_block_trade_stats",
)


def _core() -> List[str]:
    """9 个能力声明的全部技能工具（18 个）。"""
    return caps.agent_tools(caps.all_names())


PROFILES: Dict[str, List[str]] = {
    "deep_report": [],        # 下面动态构建（依赖 capabilities）
    "deep_report_theme": [],
    "deep_report_full": [],
}


def _dedupe(items) -> List[str]:
    out: List[str] = []
    for x in items:
        if x and x not in out:
            out.append(x)
    return out


PROFILES["deep_report"] = _dedupe(_core() + list(RESEARCH_TOOLS))
PROFILES["deep_report_theme"] = _dedupe(PROFILES["deep_report"] + list(THEME_TOOLS))
# full = theme + 情绪盘面；**仍然不含 EXCLUDED**（持仓/策略/报告套娃永不进报告子代理）。
# EXCLUDED 是「明确不允许」的清单，不是「full 档才放开」的清单。
PROFILES["deep_report_full"] = _dedupe(PROFILES["deep_report_theme"]
                                       + list(MARKET_REVIEW_TOOLS))

DEFAULT_PROFILE = "deep_report"


def get_profile(name: str = DEFAULT_PROFILE) -> List[str]:
    key = (name or DEFAULT_PROFILE).strip()
    if key not in PROFILES:
        raise KeyError(f"未知工具 profile: {name!r}（可选：{list(PROFILES)}）")
    return list(PROFILES[key])


def profile_summary() -> Dict[str, int]:
    return {k: len(v) for k, v in PROFILES.items()}

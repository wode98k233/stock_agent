"""
分析框架引擎 — Scenario → Template 映射与 EvidenceBag 适配器

将场景路由器的 handler 输出转换为分析框架可消费的 EvidenceBag 格式。
"""
import json
import logging
from typing import Optional

import pandas as pd

from agents.scenario_router import Scenario

logger = logging.getLogger(__name__)

# 场景 → 报告模板映射表
# 有模板的场景走 run_analysis() 后处理增强，无模板的场景跳过
SCENARIO_TEMPLATE_MAP: dict[Scenario, Optional[str]] = {
    Scenario.STOCK_ANALYSIS: "stock_deep_dive",
    Scenario.SECTOR_ANALYSIS: "sector_timing",
    Scenario.MARKET_OVERVIEW: "market_daily",
    Scenario.COMPARISON: "stock_comparison",
    Scenario.SCREENING: None,       # 选股输出是列表，模板增强价值有限
    Scenario.DATA_QUERY: None,      # 纯数据查询，不需要模板
}


def get_template_id_for_scenario(scenario: Scenario) -> Optional[str]:
    """根据场景类型获取对应的报告模板 ID"""
    return SCENARIO_TEMPLATE_MAP.get(scenario)


def _safe_serialize(obj) -> str:
    """安全序列化，DataFrame 转为 records 格式的 JSON"""
    if isinstance(obj, pd.DataFrame):
        return json.dumps(obj.to_dict(orient="records"), ensure_ascii=False, default=str)
    return json.dumps(obj, ensure_ascii=False, default=str)


def _make_item(tool_name: str, raw_output: str, tool_input: str = "") -> dict:
    """构建标准 EvidenceItem dict"""
    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "raw_output": raw_output,
        "output_preview": raw_output[:500],
        "output_length": len(raw_output),
        "parsed_json": None,
        "tables": [],
        "created_at": None,
        "truncated": len(raw_output) > 10000,
    }


def _try_parse_json(raw: str) -> any:
    """尝试解析 JSON，失败返回 None"""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _news_list_to_text(news_list: list) -> str:
    """将新闻列表拼接为可读文本，供 EvidenceBag.news_text 使用"""
    parts = []
    for n in news_list:
        title = n.get("title", "")
        source = n.get("source", "")
        time_str = n.get("time", "")
        parts.append(f"- {title} ({source} {time_str})")
    return "\n".join(parts)


def _build_stock_analysis_evidence(handler_data: dict) -> dict:
    """个股分析场景：将 handler 数据包转换为 EvidenceBag

    数据源映射：
    - data.realtime → market_data（实时行情）
    - data.rating → stock_rating（评级数据）
    - data.financial → financial_data（财务指标）
    - tech → technical_indicator（技术指标）
    - sentiment → sentiment_analysis（情感分析）
    - analysis → llm_analysis（LLM 分析结论）
    """
    items = []
    raw_text = ""
    news_text = ""
    json_fragments = []
    normalized_fields = {}
    table_fragments = []

    stock_code = handler_data.get("stock_code", "")
    stock_name = handler_data.get("stock_name", "")
    data = handler_data.get("data", {})

    normalized_fields["stock_code"] = stock_code
    normalized_fields["stock_name"] = stock_name

    rt = data.get("realtime", {})
    if rt:
        raw = _safe_serialize(rt)
        item = _make_item("stock_analysis/realtime", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/realtime ---\n{raw}"
        for key in ("price", "pct_chg", "pe", "pb", "amount", "turnover_rate", "total_mv"):
            if key in rt and rt[key] is not None:
                normalized_fields[key] = rt[key]

    history = data.get("history")
    if history is not None:
        raw = _safe_serialize(history)
        item = _make_item("stock_analysis/history", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/history ---\n{raw[:2000]}"

    news = data.get("news", [])
    if news:
        raw = _safe_serialize(news)
        item = _make_item("stock_analysis/news", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/news ---\n{raw[:2000]}"
        news_text = _news_list_to_text(news)

    rating = data.get("rating", {})
    if rating:
        raw = _safe_serialize(rating)
        item = _make_item("stock_analysis/rating", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/rating ---\n{raw}"
        if "rating_summary" in rating:
            normalized_fields["rating_summary"] = rating["rating_summary"]

    financial = data.get("financial", {})
    if financial:
        raw = _safe_serialize(financial)
        item = _make_item("stock_analysis/financial", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/financial ---\n{raw}"

    tech = handler_data.get("tech", {})
    if tech:
        raw = _safe_serialize(tech)
        item = _make_item("stock_analysis/tech", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/tech ---\n{raw}"
        for key in ("macd", "dif", "dea", "rsi_6", "rsi_14", "kdj_k", "kdj_d",
                     "ma5", "ma10", "ma20", "ma60", "close"):
            if key in tech and tech[key] is not None:
                normalized_fields[key] = tech[key]

    sentiment = handler_data.get("sentiment", {})
    if sentiment:
        raw = _safe_serialize(sentiment)
        item = _make_item("stock_analysis/sentiment", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/sentiment ---\n{raw}"
        for key in ("total_score", "conclusion"):
            if key in sentiment and sentiment[key] is not None:
                normalized_fields[f"sentiment_{key}"] = sentiment[key]

    analysis = handler_data.get("analysis", {})
    if analysis:
        raw = _safe_serialize(analysis)
        item = _make_item("stock_analysis/llm_analysis", raw, stock_code)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- stock_analysis/llm_analysis ---\n{raw}"
        for key in ("conclusion_short", "conclusion_mid", "risk", "tech_analysis",
                     "news_summary", "fundamental"):
            if key in analysis and analysis[key]:
                normalized_fields[key] = analysis[key]

    for item in items:
        if item["parsed_json"] is not None:
            json_fragments.append({"tool": item["tool_name"], "data": item["parsed_json"]})

    return {
        "items": items,
        "raw_text": raw_text,
        "news_text": news_text,
        "json_fragments": json_fragments,
        "normalized_fields": normalized_fields,
        "table_fragments": table_fragments,
        "metadata": {"tool_count": len(items), "truncated_count": sum(1 for i in items if i["truncated"])},
    }


def _build_sector_analysis_evidence(handler_data: dict) -> dict:
    """板块分析场景：将 handler 数据包转换为 EvidenceBag

    数据源映射：
    - sector_quote → sector_quote（板块行情）
    - top_stocks → sector_top_stocks（领涨股）
    - news → sector_news（板块新闻）
    - driver_analysis → sector_driver（驱动分析）
    """
    items = []
    raw_text = ""
    news_text = ""
    json_fragments = []
    normalized_fields = {}
    table_fragments = []

    sector_name = handler_data.get("sector_name", "")
    normalized_fields["sector_name"] = sector_name

    sector_quote = handler_data.get("sector_quote", {})
    if sector_quote:
        raw = _safe_serialize(sector_quote)
        item = _make_item("sector_analysis/quote", raw, sector_name)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- sector_analysis/quote ---\n{raw}"
        for key in ("pct_chg", "amount", "pct_chg_5d"):
            if key in sector_quote and sector_quote[key] is not None:
                normalized_fields[key] = sector_quote[key]

    top_stocks = handler_data.get("top_stocks", [])
    if top_stocks:
        raw = _safe_serialize(top_stocks)
        item = _make_item("sector_analysis/top_stocks", raw, sector_name)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- sector_analysis/top_stocks ---\n{raw}"
        table_fragments.append({"source": "sector_top_stocks", "rows": top_stocks})

    news = handler_data.get("news", [])
    if news:
        raw = _safe_serialize(news)
        item = _make_item("sector_analysis/news", raw, sector_name)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- sector_analysis/news ---\n{raw[:2000]}"
        news_text = _news_list_to_text(news)

    driver_analysis = handler_data.get("driver_analysis", "")
    if driver_analysis:
        raw = driver_analysis if isinstance(driver_analysis, str) else _safe_serialize(driver_analysis)
        item = _make_item("sector_analysis/driver", raw, sector_name)
        items.append(item)
        raw_text += f"\n--- sector_analysis/driver ---\n{raw}"
        normalized_fields["driver_analysis"] = raw

    for item in items:
        if item["parsed_json"] is not None:
            json_fragments.append({"tool": item["tool_name"], "data": item["parsed_json"]})

    return {
        "items": items,
        "raw_text": raw_text,
        "news_text": news_text,
        "json_fragments": json_fragments,
        "normalized_fields": normalized_fields,
        "table_fragments": table_fragments,
        "metadata": {"tool_count": len(items), "truncated_count": sum(1 for i in items if i["truncated"])},
    }


def _build_market_overview_evidence(handler_data: dict) -> dict:
    """市场概览场景：将 handler 数据包转换为 EvidenceBag

    数据源映射：
    - index_data → market_index（主要指数行情）
    - breadth → market_breadth（市场宽度）
    - hot_sectors → hot_sectors（热门板块）
    - cold_sectors → cold_sectors（冷门板块）
    - summary → market_summary（市场综述）
    """
    items = []
    raw_text = ""
    news_text = ""
    json_fragments = []
    normalized_fields = {}
    table_fragments = []

    index_data = handler_data.get("index_data", [])
    if index_data:
        raw = _safe_serialize(index_data)
        item = _make_item("market_overview/index", raw)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- market_overview/index ---\n{raw}"
        for idx in index_data:
            name = idx.get("name", "")
            if name:
                normalized_fields[f"index_{name}_price"] = idx.get("price")
                normalized_fields[f"index_{name}_pct_chg"] = idx.get("pct_chg")

    breadth = handler_data.get("breadth", {})
    if breadth:
        raw = _safe_serialize(breadth)
        item = _make_item("market_overview/breadth", raw)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- market_overview/breadth ---\n{raw}"
        for key in ("up", "down", "flat", "limit_up", "limit_down"):
            if key in breadth:
                normalized_fields[f"breadth_{key}"] = breadth[key]

    hot_sectors = handler_data.get("hot_sectors", [])
    if hot_sectors:
        raw = _safe_serialize(hot_sectors)
        item = _make_item("market_overview/hot_sectors", raw)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- market_overview/hot_sectors ---\n{raw}"
        table_fragments.append({"source": "hot_sectors", "rows": hot_sectors})

    cold_sectors = handler_data.get("cold_sectors", [])
    if cold_sectors:
        raw = _safe_serialize(cold_sectors)
        item = _make_item("market_overview/cold_sectors", raw)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- market_overview/cold_sectors ---\n{raw}"
        table_fragments.append({"source": "cold_sectors", "rows": cold_sectors})

    summary = handler_data.get("summary", "")
    if summary:
        raw = summary if isinstance(summary, str) else _safe_serialize(summary)
        item = _make_item("market_overview/summary", raw)
        items.append(item)
        raw_text += f"\n--- market_overview/summary ---\n{raw}"
        normalized_fields["market_summary"] = raw

    for item in items:
        if item["parsed_json"] is not None:
            json_fragments.append({"tool": item["tool_name"], "data": item["parsed_json"]})

    return {
        "items": items,
        "raw_text": raw_text,
        "news_text": news_text,
        "json_fragments": json_fragments,
        "normalized_fields": normalized_fields,
        "table_fragments": table_fragments,
        "metadata": {"tool_count": len(items), "truncated_count": sum(1 for i in items if i["truncated"])},
    }


def _build_comparison_evidence(handler_data: dict) -> dict:
    """对比分析场景：将 handler 数据包转换为 EvidenceBag

    数据源映射：
    - valid[i] 的 realtime → {stock_name}_market_data（各股行情）
    - valid[i] 的 tech → {stock_name}_tech（各股技术指标）
    - metrics → comparison_metrics（指标对比表）
    - conclusion → comparison_conclusion（对比结论）
    """
    items = []
    raw_text = ""
    news_text = ""
    json_fragments = []
    normalized_fields = {}
    table_fragments = []

    valid = handler_data.get("valid", [])
    for entry in valid:
        stock = entry[0] if len(entry) > 0 else {}
        bundle = entry[1] if len(entry) > 1 else {}
        tech = entry[2] if len(entry) > 2 else {}

        code = stock.get("code", "")
        name = stock.get("name", "")

        rt = bundle.get("realtime", {})
        if rt:
            raw = _safe_serialize(rt)
            item = _make_item(f"comparison/{name}/realtime", raw, code)
            item["parsed_json"] = _try_parse_json(raw)
            items.append(item)
            raw_text += f"\n--- comparison/{name}/realtime ---\n{raw}"

        if tech:
            raw = _safe_serialize(tech)
            item = _make_item(f"comparison/{name}/tech", raw, code)
            item["parsed_json"] = _try_parse_json(raw)
            items.append(item)
            raw_text += f"\n--- comparison/{name}/tech ---\n{raw}"

        normalized_fields[f"{name}_price"] = rt.get("price")
        normalized_fields[f"{name}_pct_chg"] = rt.get("pct_chg")
        normalized_fields[f"{name}_pe"] = rt.get("pe")
        normalized_fields[f"{name}_macd"] = tech.get("macd")
        normalized_fields[f"{name}_rsi_14"] = tech.get("rsi_14")

    metrics = handler_data.get("metrics", [])
    if metrics:
        raw = _safe_serialize(metrics)
        item = _make_item("comparison/metrics", raw)
        item["parsed_json"] = _try_parse_json(raw)
        items.append(item)
        raw_text += f"\n--- comparison/metrics ---\n{raw}"
        table_fragments.append({"source": "comparison_metrics", "rows": metrics})

    conclusion = handler_data.get("conclusion", "")
    if conclusion:
        raw = conclusion if isinstance(conclusion, str) else _safe_serialize(conclusion)
        item = _make_item("comparison/conclusion", raw)
        items.append(item)
        raw_text += f"\n--- comparison/conclusion ---\n{raw}"
        normalized_fields["comparison_conclusion"] = raw

    for item in items:
        if item["parsed_json"] is not None:
            json_fragments.append({"tool": item["tool_name"], "data": item["parsed_json"]})

    return {
        "items": items,
        "raw_text": raw_text,
        "news_text": news_text,
        "json_fragments": json_fragments,
        "normalized_fields": normalized_fields,
        "table_fragments": table_fragments,
        "metadata": {"tool_count": len(items), "truncated_count": sum(1 for i in items if i["truncated"])},
    }


# 场景 → 证据构建函数映射（策略模式，避免 if-else 分支）
_BUILDERS = {
    Scenario.STOCK_ANALYSIS: _build_stock_analysis_evidence,
    Scenario.SECTOR_ANALYSIS: _build_sector_analysis_evidence,
    Scenario.MARKET_OVERVIEW: _build_market_overview_evidence,
    Scenario.COMPARISON: _build_comparison_evidence,
}


def build_evidence_from_scenario(scenario: Scenario, handler_data: dict) -> dict:
    """将场景 handler 的数据包转换为 EvidenceBag，供 run_analysis() 消费

    通过 _BUILDERS 策略字典分发到各场景专用构建函数，
    无适配器的场景返回空 EvidenceBag。
    """
    builder = _BUILDERS.get(scenario)
    if builder is None:
        logger.info(f"📦 场景 {scenario.value} 无适配器，返回空 EvidenceBag")
        return {
            "items": [],
            "raw_text": "",
            "news_text": "",
            "json_fragments": [],
            "normalized_fields": {},
            "table_fragments": [],
            "metadata": {"tool_count": 0, "truncated_count": 0},
        }
    return builder(handler_data)

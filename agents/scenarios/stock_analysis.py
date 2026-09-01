"""
选股雷达 - 个股深度分析场景处理器

用户说"分析长江电力未来走势"时的执行流程：
1. 识别股票代码
2. 并行获取：行情、K线、新闻、评级、财务（fetch_stock_bundle）
3. 计算技术指标（复用 bundle 中的 history）
4. 情感分析（复用 bundle 中的 news）
5. LLM综合分析
6. 格式化输出
"""
import json
import traceback
import logging
from typing import Optional
from output.formatter import section_header, sub_header, text_block, data_time_tag, kv_line
from output.time_util import format_news_time

logger = logging.getLogger("radar.scenario")

# ── 提示词（从 agents/prompts.py 迁入）────────────────────────
STOCK_ANALYSIS_PROMPT = """你是专业的股票分析师。根据以下数据，对{stock_name}({stock_code})进行多维度分析。

## 用户问题
{user_input}

## 行情数据
{rt_json}

## 技术指标
{tech_json}

## 机构评级
{rating_json}

## 近期新闻
{news_json}

## 新闻情感
{sentiment_json}

请返回JSON格式：
{{
  "tech_analysis": "技术面分析（2-3句话，包含MACD/RSI/均线状态和含义）",
  "news_summary": "消息面分析（2-3句话，近期新闻要点和情感倾向）",
  "fundamental": "基本面分析（2-3句话，PE/PB/股息率/机构评级）",
  "conclusion_short": "短期判断（1-2周，给出方向和关键价位）",
  "conclusion_mid": "中期判断（1-3月，给出方向和逻辑）",
  "risk": "风险提示（1-2句话）"
}}

要求：
1. 必须基于提供的数据，不要编造数据
2. 给出明确的方向判断（看多/看空/震荡），不要含糊
3. 如果某个维度数据缺失，标注"数据不足"并跳过
4. 技术面要说明指标的具体含义，不要只报数字"""


async def handle_stock_analysis(
    user_input: str,
    enriched_input: str,
    context: dict,
    data_timestamp: str,
    budget=None,
) -> Optional["ScenarioResult"]:
    """处理个股深度分析场景"""
    from agents.scenarios.common import resolve_stock, fetch_stock_bundle, BaseScenarioHandler, ScenarioResult
    from tools.sentiment import analyze_sentiment

    # 1. 识别股票
    stock_code, stock_name = resolve_stock(context)
    if not stock_code:
        return None

    # 2. 并行获取全部数据（一次调用，5路并行）
    data = await fetch_stock_bundle(stock_code)

    # 3. 计算技术指标（复用已获取的 history，不重复IO）
    tech = BaseScenarioHandler.calc_tech_indicators(data)

    # 4. 情感分析（复用已获取的 news，不重复IO）
    sentiment = {"total_score": 0, "conclusion": "无新闻", "summary": "暂无相关新闻"}
    if data["news"]:
        try:
            sentiment = analyze_sentiment(data["news"], None, logger) or sentiment
        except Exception as e:
            logger.warning(f"情感分析失败: {e} {traceback.format_exc()}")

    # 5. LLM综合分析（使用 enriched_input）
    analysis = await _synthesize_analysis(
        enriched_input, stock_name, stock_code, data, tech, sentiment, budget
    )

    # 6. 格式化输出
    return ScenarioResult(
        text=_format_output(
            stock_name, stock_code, data, tech, sentiment, analysis, data_timestamp
        ),
        data={
            "stock_code": stock_code,
            "stock_name": stock_name,
            "data": {k: v for k, v in data.items() if k != "history"},
            "tech": tech,
            "sentiment": sentiment,
            "analysis": analysis,
        },
    )


async def _synthesize_analysis(
    user_input: str,
    stock_name: str,
    stock_code: str,
    data: dict,
    tech: dict,
    sentiment: dict,
    budget=None,
) -> dict:
    """LLM综合分析，生成各维度解读和综合判断"""
    from agents.scenarios.common import BaseScenarioHandler

    rt = data.get("realtime", {})
    rating = data.get("rating", {})
    news = data.get("news", [])

    tech_summary = {}
    if tech:
        for key in ["macd", "dif", "dea", "rsi_6", "rsi_14", "kdj_k", "kdj_d",
                     "ma5", "ma10", "ma20", "ma60", "close"]:
            if key in tech:
                tech_summary[key] = tech[key]

    news_brief = []
    for n in news[:5]:
        news_brief.append({
            "title": n.get("title", ""),
            "time": n.get("time", ""),
            "source": n.get("source", ""),
        })

    prompt = STOCK_ANALYSIS_PROMPT.format(
        stock_name=stock_name,
        stock_code=stock_code,
        user_input=user_input,
        rt_json=json.dumps(rt, ensure_ascii=False, default=str),
        tech_json=json.dumps(tech_summary, ensure_ascii=False, default=str),
        rating_json=json.dumps(rating, ensure_ascii=False, default=str),
        news_json=json.dumps(news_brief, ensure_ascii=False, default=str),
        sentiment_json=json.dumps(sentiment, ensure_ascii=False, default=str),
    )
    result = await BaseScenarioHandler.call_llm(prompt, logger, "stock-analysis", budget=budget, json_mode=True)
    if result:
        return result
    return {}


def _format_output(
    stock_name: str,
    stock_code: str,
    data: dict,
    tech: dict,
    sentiment: dict,
    analysis: dict,
    data_timestamp: str,
) -> str:
    """格式化输出"""
    from agents.scenarios.common import format_amount, format_pct

    lines = [section_header("个股分析", f"{stock_name} ({stock_code})")]

    # 行情快照
    rt = data.get("realtime", {})
    if rt:
        lines.append(sub_header("行情快照"))
        lines.append(kv_line("现价", f"{rt.get('price', 0):.2f}"))
        lines.append(kv_line("涨幅", format_pct(rt.get("pct_chg", 0))))
        lines.append(kv_line("成交额", format_amount(rt.get("amount", 0))))
        lines.append(kv_line("换手率", f"{rt.get('turnover_rate', 0):.2f}%"))

    # 技术面
    tech_text = analysis.get("tech_analysis", "")
    if tech_text:
        lines.append(sub_header("技术面"))
        # 附带关键指标数值
        if tech:
            indicators = []
            if "macd" in tech:
                macd_val = tech["macd"]
                macd_str = f"+{macd_val:.3f}" if macd_val >= 0 else f"{macd_val:.3f}"
                indicators.append(f"MACD: {macd_str}")
            if "rsi_14" in tech:
                indicators.append(f"RSI(14): {tech['rsi_14']:.1f}")
            if indicators:
                lines.append(f"  指标: {' | '.join(indicators)}")
        lines.append(text_block(tech_text))

    # 消息面
    news_text = analysis.get("news_summary", "")
    if news_text:
        lines.append(sub_header("消息面"))
        lines.append(text_block(news_text))
        # 列出近期新闻（带时间）
        news = data.get("news", [])
        if news:
            lines.append("")
            lines.append("  近期新闻:")
            for i, n in enumerate(news[:3], 1):
                title = n.get("title", "")
                time_str = format_news_time(n.get("time", ""))
                source = n.get("source", "")
                lines.append(f"  {i}. {title}  ({time_str})")

    # 基本面
    fundamental_text = analysis.get("fundamental", "")
    if fundamental_text:
        lines.append(sub_header("基本面"))
        # 附带关键财务指标
        rt_local = data.get("realtime", {})
        rating = data.get("rating", {})
        if rt_local:
            metrics = []
            pe = rt_local.get("pe", 0)
            if pe and pe > 0:
                metrics.append(f"PE: {pe:.1f}")
            pb = rt_local.get("pb", 0)
            if pb and pb > 0:
                metrics.append(f"PB: {pb:.1f}")
            if metrics:
                lines.append(f"  指标: {' | '.join(metrics)}")
        if rating and rating.get("rating_summary"):
            lines.append(f"  评级: {rating['rating_summary']}")
        lines.append(text_block(fundamental_text))

    # 综合判断
    conclusion_short = analysis.get("conclusion_short", "")
    conclusion_mid = analysis.get("conclusion_mid", "")
    risk = analysis.get("risk", "")

    if conclusion_short or conclusion_mid:
        lines.append(sub_header("综合判断"))
        if conclusion_short:
            lines.append(f"  短期(1-2周): {conclusion_short}")
        if conclusion_mid:
            lines.append(f"  中期(1-3月): {conclusion_mid}")
        if risk:
            lines.append(f"  ⚠ 风险: {risk}")

    lines.append(data_time_tag(data_timestamp))
    return "\n".join(lines)

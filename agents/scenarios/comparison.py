"""
选股雷达 - 对比分析场景处理器

用户说"长江电力和华能国际哪个好"时的执行流程：
1. 解析所有股票（最多5只）
2. 并行获取所有股票的行情和技术指标
3. LLM对比分析
4. 格式化输出
"""
import asyncio
import json
import logging
from typing import Optional

from agents.scenarios.common import (
    ScenarioResult,
    fetch_stock_bundle,
    format_market_cap,
    format_pct,
    resolve_stocks,
)

# ── 提示词（从 agents/prompts.py 迁入）────────────────────────
COMPARISON_PROMPT = """你是专业的股票分析师。对比分析以下股票。

## 用户问题
{user_input}

{stock_sections}

请给出对比结论（200字以内），包含：
1. 各自的优势
2. 适合什么样的投资者
3. 明确推荐排名

要求给出明确推荐，不要含糊。"""


logger = logging.getLogger("radar.scenario")


async def handle_comparison(
    user_input: str,
    enriched_input: str,
    context: dict,
    data_timestamp: str,
    budget=None,
) -> Optional["ScenarioResult"]:
    """处理对比分析场景"""

    # 1. 解析所有股票（最多5只）
    stocks = resolve_stocks(context, max_count=5)
    if len(stocks) < 2:
        return None

    # 2. 并行获取所有股票数据
    bundles = await asyncio.gather(
        *[fetch_stock_bundle(s["code"]) for s in stocks],
        return_exceptions=True,
    )

    # 3. 组装有效数据
    valid = []
    for stock, bundle in zip(stocks, bundles):
        if isinstance(bundle, Exception):
            logger.warning(f"⚠️ {stock['code']} 数据获取失败: {bundle}")
            continue
        from agents.scenarios.common import BaseScenarioHandler
        tech = BaseScenarioHandler.calc_tech_indicators(bundle)
        valid.append((stock, bundle, tech))

    if len(valid) < 2:
        return None

    # 4. 构建对比指标
    metrics = _build_metrics(valid)

    # 5. LLM分析（使用 enriched_input）
    conclusion = await _compare_analysis(enriched_input, valid, budget)

    # 6. 格式化输出
    return ScenarioResult(
        text=_format_output(valid, metrics, conclusion, data_timestamp),
        data={
            "valid": [
                {
                    "stock": {"code": s["code"], "name": s["name"]},
                    "realtime": b.get("realtime", {}),
                    "tech": t,
                }
                for s, b, t in valid
            ],
            "metrics": metrics,
            "conclusion": conclusion,
        },
    )


def _build_metrics(valid: list) -> list:
    """构建对比指标表"""
    metrics = []
    labels = ["现价", "今日涨幅", "PE(动态)", "PB", "换手率", "MACD", "RSI(14)", "总市值"]

    for label in labels:
        row = {"label": label, "values": []}
        for stock, bundle, tech in valid:
            rt = bundle.get("realtime", {})
            if label == "现价":
                row["values"].append(f"{rt.get('price', '-')}")
            elif label == "今日涨幅":
                pct = rt.get("pct_chg", 0)
                row["values"].append(format_pct(pct))
            elif label == "PE(动态)":
                pe = rt.get("pe", 0)
                row["values"].append(f"{pe:.1f}" if pe and pe > 0 else "-")
            elif label == "PB":
                pb = rt.get("pb", 0)
                row["values"].append(f"{pb:.2f}" if pb and pb > 0 else "-")
            elif label == "换手率":
                row["values"].append(f"{rt.get('turnover_rate', 0):.2f}%")
            elif label == "MACD":
                macd = tech.get("macd", 0)
                row["values"].append("金叉" if macd > 0 else "死叉")
            elif label == "RSI(14)":
                rsi = tech.get("rsi_14", 0)
                row["values"].append(f"{rsi:.1f}" if rsi else "-")
            elif label == "总市值":
                mv = rt.get("total_mv", 0)
                row["values"].append(format_market_cap(mv) if mv > 10000 else f"{mv}")
        metrics.append(row)
    return metrics


async def _compare_analysis(user_input: str, valid: list, budget=None) -> str:
    """LLM对比分析"""
    from agents.scenarios.common import BaseScenarioHandler

    sections = []
    for stock, bundle, tech in valid:
        rt = bundle.get("realtime", {})
        tech_brief = {k: tech.get(k) for k in ["macd", "rsi_14", "ma5", "ma20"] if k in tech}
        sections.append(
            f"## {stock['name']}({stock['code']})\n"
            f"行情: {json.dumps(rt, ensure_ascii=False, default=str)}\n"
            f"技术指标: {json.dumps(tech_brief, ensure_ascii=False, default=str)}"
        )

    prompt = COMPARISON_PROMPT.format(
        user_input=user_input,
        stock_sections="\n".join(sections),
    )
    return await BaseScenarioHandler.call_llm(prompt, logger, "comparison-analysis", budget=budget)


def _format_output(valid: list, metrics: list, conclusion: str, data_timestamp: str) -> str:
    """格式化对比输出"""
    names = " vs ".join(f"{s['name']}" for s, _, _ in valid)

    lines = []
    lines.append("=" * 60)
    lines.append(f"📊 对比分析: {names}")
    lines.append("-" * 60)

    # 使用 formatters 里的中文对齐表格
    from agents.scenarios.formatters import format_comparison_table
    table = format_comparison_table(valid, metrics)
    lines.append(table)

    lines.append("-" * 60)
    lines.append("📝 分析结论:")
    lines.append(conclusion)
    lines.append(f"\n⏰ 数据时间: {data_timestamp}")
    lines.append("=" * 60)

    return "\n".join(lines)

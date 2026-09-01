"""
选股雷达 - 市场概览场景处理器

用户说"给我总结今天的股市早报"时的执行流程：
1. 获取大盘指数行情
2. 获取涨跌统计
3. 获取热门/冷门板块
4. LLM简评
5. 格式化输出
"""
import json
import logging
from typing import Optional
from output.formatter import render_market_overview

logger = logging.getLogger("radar.scenario")

# ── 提示词（从 agents/prompts.py 迁入）────────────────────────
MARKET_OVERVIEW_PROMPT = """你是专业的市场分析师。根据以下数据，生成今日A股市场简评。

## 用户问题
{user_input}

## 大盘指数
{index_data_json}

## 涨跌统计
{breadth_json}

## 涨幅前5板块
{hot_sectors_json}

请生成2-3句话的市场简评，包含：
1. 市场整体走势判断
2. 主要热点方向
3. 短期关注点

要求简洁有力，控制在100字以内。"""


async def handle_market_overview(
    user_input: str,
    enriched_input: str,
    context: dict,
    data_timestamp: str,
    budget=None,
) -> Optional["ScenarioResult"]:
    """处理市场概览场景"""
    import asyncio
    from tools.fetcher import ak_spot_em
    from agents.scenarios.common import ScenarioResult

    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(None, ak_spot_em)
    except Exception as e:
        logger.error(f"行情数据获取失败: {e}")
        return None

    if df is None or df.empty:
        return None

    index_data = _extract_index_data(df)
    breadth = _compute_breadth(df)
    hot_sectors, cold_sectors = _get_sector_ranking()

    summary = await _generate_summary(enriched_input, index_data, breadth, hot_sectors)

    return ScenarioResult(
        text=render_market_overview(
            index_data=index_data,
            breadth=breadth,
            hot_sectors=hot_sectors,
            cold_sectors=cold_sectors,
            summary=summary,
            data_timestamp=data_timestamp,
        ),
        data={
            "index_data": index_data,
            "breadth": breadth,
            "hot_sectors": hot_sectors,
            "cold_sectors": cold_sectors,
            "summary": summary,
        },
    )


def _extract_index_data(df) -> list:
    """从全市场行情DataFrame中提取大盘指数"""
    index_names = {"上证": "上证指数", "深证": "深证成指", "创业板": "创业板指"}
    result = []
    for _, row in df.iterrows():
        name = str(row.get("名称", ""))
        for key, display in index_names.items():
            if key in name:
                result.append({
                    "name": display,
                    "price": float(row.get("最新价", 0)),
                    "pct_chg": float(row.get("涨跌幅", 0)),
                    "amount": float(row.get("成交额", 0)),
                })
                break
    return result


def _compute_breadth(df) -> dict:
    """从全市场行情DataFrame中计算涨跌统计"""
    pct_col = "涨跌幅"
    if pct_col not in df.columns:
        return {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0}

    pcts = df[pct_col].dropna()
    return {
        "up": int((pcts > 0).sum()),
        "down": int((pcts < 0).sum()),
        "flat": int((pcts == 0).sum()),
        "limit_up": int((pcts >= 9.9).sum()),
        "limit_down": int((pcts <= -9.9).sum()),
    }


def _get_sector_ranking() -> tuple:
    """获取板块涨跌排行"""
    hot = []
    cold = []

    try:
        from tools.stock_data import get_industry_list
        df = get_industry_list(logger)
        if df is None or df.empty:
            return hot, cold

        if '涨跌幅' in df.columns:
            sorted_df = df.sort_values('涨跌幅', ascending=False)

            for _, row in sorted_df.head(5).iterrows():
                hot.append({
                    "name": str(row.get('板块名称', row.get('名称', ''))),
                    "pct_chg": float(row.get('涨跌幅', 0)),
                })

            for _, row in sorted_df.tail(5).iterrows():
                cold.append({
                    "name": str(row.get('板块名称', row.get('名称', ''))),
                    "pct_chg": float(row.get('涨跌幅', 0)),
                })
    except Exception as e:
        logger.warning(f"获取板块排行失败: {e}")

    return hot, cold


async def _generate_summary(
    user_input: str,
    index_data: list,
    breadth: dict,
    hot_sectors: list,
    budget=None,
) -> str:
    """LLM生成市场简评"""
    from agents.scenarios.common import BaseScenarioHandler

    prompt = MARKET_OVERVIEW_PROMPT.format(
        user_input=user_input,
        index_data_json=json.dumps(index_data, ensure_ascii=False, default=str),
        breadth_json=json.dumps(breadth, ensure_ascii=False, default=str),
        hot_sectors_json=json.dumps(hot_sectors, ensure_ascii=False, default=str),
    )
    return await BaseScenarioHandler.call_llm(prompt, logger, "market-summary", budget=budget)

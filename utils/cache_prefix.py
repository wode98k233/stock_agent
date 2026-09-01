"""
LLM 缓存前缀 (Prompt Caching)

为指定 LLM 调用注入固定前缀，利用 OpenAI/DeepSeek 等 API 的 prompt caching 机制。
缓存命中的 token 费用降低 50-90%，延迟也更低。

Usage:
    from utils.cache_prefix import inject_cache_prefix

    # 在 invoke 前注入
    messages = inject_cache_prefix(messages, skip=False)
"""

from __future__ import annotations

import logging

logger = logging.getLogger("radar.cache_prefix")


# ─── 默认前缀 (~300 tokens, 覆盖 A 股通用知识) ─────────────

DEFAULT_CACHE_PREFIX = """## 系统上下文
你是「选股雷达」——A 股智能分析系统。

### A 股市场规则
- 交易时间: 09:30-11:30, 13:00-15:00 (周一至周五)
- 涨跌幅限制: 主板±10%, 创业板/科创板±20%, ST±5%, 北交所±30%
- T+1 交易制度，当日买入次日方可卖出
- 货币: 人民币 (CNY)，价格单位: 元
- 市场分类: 沪市 (60xxxx), 深市 (00xxxx/30xxxx), 北交所 (8xxxxx/4xxxxx)

### 数据解读规范
- 成交量: 以手为单位 (1手=100股)，关注量比和换手率
- 资金流向: 正值为净流入，负值为净流出，单位通常为万元或亿元
- 技术指标: MACD金叉/死叉、RSI超买(>70)/超卖(<30)、KDJ交叉
- 估值: PE/PB 需结合行业均值和历史百分位判断
- 新闻情感: 正面/中性/负面，注意时效性（近期权重更高）

### 输出规范
- 使用中文，专业术语可附英文
- 价格精确到小数点后两位，百分比保留一位小数
- 日期格式: YYYY-MM-DD
- 数据结论必须注明来源和时效性
- 不提供具体买卖建议，仅提供分析参考
- 风险提示必须前置，不隐瞒潜在风险"""


def get_cache_prefix() -> str:
    """
    获取缓存前缀内容。

    优先使用 CACHE_PREFIX_CONTENT 自定义内容，否则使用默认前缀。
    """
    from config import Config

    custom = getattr(Config, "CACHE_PREFIX_CONTENT", "")
    return custom if custom else DEFAULT_CACHE_PREFIX


def should_inject_prefix() -> bool:
    """检查是否启用缓存前缀"""
    from config import Config
    return getattr(Config, "CACHE_PREFIX_ENABLED", False)


def inject_cache_prefix(messages: list, skip: bool = False) -> list:
    """
    在消息列表前插入缓存前缀。

    Args:
        messages: 原始消息列表
        skip: True 时跳过注入 (用于 classify 等轻量调用)

    Returns:
        注入前缀后的消息列表，或原始列表
    """
    if skip:
        return messages

    if not should_inject_prefix():
        return messages

    prefix = get_cache_prefix()
    if not prefix:
        return messages

    return [("system", prefix), *messages]

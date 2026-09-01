"""ReAct Agent 工具函数

从 nodes.py 中提取的共享工具函数和常量：
- PerToolRateLimiter: 按工具名限流
- 去重相关: _collect_executed_tool_sigs
- 输出精简/压缩: _trim_tool_output, _compress_tool_output
- 历史消息清理: clean_history_messages
- 常量: _TRIM_FIELDS_MX_XUANGU, _TRIM_FIELDS_MX_SEARCH, _COMPRESS_SYSTEM_PROMPT
"""
import asyncio
import json
import time
from collections import defaultdict
from typing import Set, Callable, Awaitable

from langchain_core.messages import AIMessage, ToolMessage

from utils.llm_factory import get_compress_llm


class PerToolRateLimiter:
    """按工具名限流：同名工具串行间隔，不同名工具互不阻塞。"""

    def __init__(
        self,
        min_interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._locks = defaultdict(asyncio.Lock)
        self._last_started = {}

    async def wait(self, tool_name: str):
        if self.min_interval_seconds <= 0:
            return

        async with self._locks[tool_name]:
            now = self._clock()
            last_started = self._last_started.get(tool_name)
            if last_started is not None:
                delay = self.min_interval_seconds - (now - last_started)
                if delay > 0:
                    await self._sleep(delay)
            self._last_started[tool_name] = self._clock()


def _collect_executed_tool_sigs(messages: list) -> Set[str]:
    """从消息历史中收集已执行的 tool_name:args 签名（用于迭代间去重提示）"""
    sigs = set()
    for msg in messages:
        # AIMessage 中的 tool_calls 记录了调用意图
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", {})
                if name:
                    sigs.add(f"{name}({json.dumps(args, ensure_ascii=False)})")
        # ToolMessage 记录了实际执行结果
        if isinstance(msg, ToolMessage) and msg.name:
            # ToolMessage 没有 args，但 tool_call_id 可以和 AIMessage 的 tool_calls 对应
            # 这里只用 name 做粗粒度过滤不够精确，跳过
            pass
    return sigs


# 冗余字段黑名单：这些字段在 mx_xuangu_filter / mx_search_news 输出中
# 占大量字符但对分析决策贡献极低，精简时移除
_TRIM_FIELDS_MX_XUANGU = {"概念"}
_TRIM_FIELDS_MX_SEARCH = {"result"}  # mx_search 的 result 字段保留，但内部做截断


def _trim_tool_output(tool_name: str, content: str) -> str:
    """精简工具输出：移除冗余字段，不截断有效数据"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content

    if not isinstance(data, dict):
        return content

    if tool_name == "mx_xuangu_filter":
        # 选股结果：移除每只股票的"概念"字段（通常几百字符，列出20+概念标签）
        stocks = data.get("stocks")
        if isinstance(stocks, list):
            for stock in stocks:
                if isinstance(stock, dict):
                    stock.pop("概念", None)

    elif tool_name == "mx_search_news":
        # 搜索结果：保留结构，result 字段中的新闻已经由 API 截断过
        # 这里不再二次截断，只移除空字段
        pass

    elif tool_name == "mx_data_query":
        # 数据查询：保留 terminal_output（LLM 主要读这个），移除冗余的 tables 原始 JSON
        # terminal_output 已经是格式化后的表格，tables 是重复的结构化数据
        if "tables" in data and "terminal_output" in data:
            data.pop("tables", None)

    return json.dumps(data, ensure_ascii=False)


_COMPRESS_SYSTEM_PROMPT = """你是一个工具输出压缩引擎。你的任务是将工具返回的超长输出压缩为简洁摘要。

关键规则（不可违反）：
1. 保留所有关键数值：价格、涨跌幅、成交量、市值、PE、PB、指标值等
2. 保留所有命名实体：股票名称、代码、板块名称、日期
3. 保留所有错误信息原文（用反引号包裹）
4. 保留所有决策相关的事实和结论
5. 省略冗余描述、重复内容、空字段、格式化标记
6. 对非关键信息（如详细列表中的次要项）进行概括
7. 绝不编造信息，绝不将具体数值泛化为模糊描述
8. 目标压缩比：3:1~5:1

{goal_hint}

输出格式：直接输出压缩后的内容，不要添加任何前缀或解释。"""


async def _compress_tool_output(tool_name: str, content: str, logger, user_goal: str = "") -> str:
    """对超长工具输出进行 LLM 压缩，保留关键信息"""
    goal_hint = ""
    if user_goal:
        goal_hint = f"用户最终目标：{user_goal}\n请优先保留与上述目标直接相关的数据，删除无关背景信息。"

    try:
        llm = get_compress_llm()
        system_prompt = _COMPRESS_SYSTEM_PROMPT.format(goal_hint=goal_hint)
        messages = [
            ("system", system_prompt),
            ("user", f"工具: {tool_name}\n\n输出内容:\n{content}"),
        ]
        config = {"metadata": {"node": "react-tool-compress"}}
        response = await llm.ainvoke(messages, config=config)
        compressed = response.content if hasattr(response, 'content') else str(response)
        if compressed and len(compressed) < len(content):
            ratio = len(content) / len(compressed) if len(compressed) > 0 else 0
            logger.debug("C", f"工具输出压缩: {tool_name} {len(content)}→{len(compressed)}字符 ({ratio:.1f}x)")
            return compressed
        logger.warning("C", f"工具输出压缩无效: {tool_name} 原文{len(content)}→压缩{len(compressed) if compressed else 0}字符，保留原文")
        return content
    except Exception as e:
        logger.warning("C", f"工具输出压缩失败: {tool_name} ({len(content)}字符), 保留原始输出: {e}")
        return content


def clean_history_messages(history: list, user_input: str) -> list:
    """清理历史消息，移除无效和重复内容。

    清理规则：
    1. 过滤空的 AI 消息（无 content 且无 tool_calls）
    2. 移除所有与 user_input 完全相同的用户消息（PrepareNode 末尾会追加当前 user）
    3. 过滤包含 "[用户问题]" 标记且内含 user_input 的消息

    Args:
        history: 原始历史消息列表（可包含 AIMessage、ToolMessage、(role, content) 元组等）
        user_input: 当前用户输入，用于识别重复内容

    Returns:
        清理后的历史消息列表
    """
    cleaned_history = []
    seen_current_user = False

    for item in history:
        if isinstance(item, AIMessage) and not item.content and not (item.tool_calls or []):
            continue

        if isinstance(item, tuple) and len(item) == 2 and item[0] in ("user", "human"):
            content = str(item[1] or "")
            if content == user_input:
                if seen_current_user:
                    continue
                seen_current_user = True
                continue
            if "[用户问题]" in content and user_input in content:
                continue

        cleaned_history.append(item)

    return cleaned_history

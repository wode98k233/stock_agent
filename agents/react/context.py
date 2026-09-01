"""ReAct 历史对话轮次压缩。

职责单一：把 messages 中冗长的 AI+Tool 轮次压缩成 1 个 summary AIMessage，
保留最近 N 轮原样输出，删除中间已被摘要替换的旧轮次。

设计要点：
1. 继承 langchain_classic.base_memory.BaseMemory
   - 对外暴露 load_memory_variables / save_context / clear / memory_variables
   - 与 LangChain memory 体系接口一致，可被外部 Chain 直接使用
   - 同时支持 LangGraph checkpoint 恢复（自身无状态，状态在 exec_state，由 LangGraph 序列化）

2. summary 放原位置（abABCD → ab[ABC]D → ab[ABC]DEF → ab[ABC][DEF]G）
   - 不放末尾，避免 LLM 把它当作独立 message 重复理解
   - 旧轮次真正"被压缩"掉，不是叠加保留

3. 触发条件（OR）
   - pending 轮次 ≥ summary_trigger_rounds
   - pending 文本字符数 ≥ summary_pending_chars

不在本模块职责内：
- 工具输出过长压缩（由 ToolNode 负责 _compress_tool_output / _trim_tool_output）
- 任何确定性"账本"输出（9 轮 trace 证明 LLM 不读，反而破坏 cache 命中率）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from langchain_core.messages import AIMessage, ToolMessage
from langchain_classic.base_memory import BaseMemory

from utils.llm_factory import llm_json_with_retry
from utils.logger import ensure_radar


# ────────────────────────────────────────────────────────────────────
# Summary 提示词 + 默认 LLM summarizer
# ────────────────────────────────────────────────────────────────────

SUMMARY_SYSTEM_PROMPT = """你是一个 ReAct 上下文压缩助手。

本任务定位：**只针对"新增的工具轮次"输出 findings**。

要求：
1. 只输出合法 JSON。
2. 字段：summary_version, summarized_rounds, rounds_summary, key_findings, data_coverage, failed_queries
3. summarized_rounds: 本轮压缩覆盖的轮次编号，用整数列表，如 [1, 2, 3]
4. rounds_summary: 简要描述本轮各轮次做了什么，**必须包含查询返回的关键数值**。
   格式："第X轮：调用[工具名]查询[内容]，返回[具体数值/关键数据]，结果成功"
   示例："第1轮：调用mx_data_query查询上证指数收盘价，返回3250.45(+0.43%)，成交额4821亿，结果成功"
   不要只写"调用了X查询了Y"——必须包含查询结果中的具体数字。
5. key_findings: 提取本轮的关键数据点，按股票/板块组织，保留具体数字和涨跌幅。
   **必须包含已查询个股的股票代码和名称（如"中际旭创(300308)"），方便后续避免重复查询。**
6. data_coverage: 本轮已成功获取的数据类别列表（用于避免后续重复查询），如 ["大盘指数行情", "涨跌家数统计", "主力资金流向", "板块涨跌幅排名"]
7. failed_queries: 本轮失败的查询和原因，格式为"工具名:查询内容 - 失败原因"。**注意：failed_queries 仅表示该特定查询未成功，不影响 key_findings 和 data_coverage 中已成功获取的数据。已成功获取的数据是可靠的，不需要重新查询。**
8. 不要重写或总结已有轮次的内容
"""


def default_summarizer(previous_summary, new_rounds_text: str, *, llm, logger, budget=None,
                       max_chars: int = 2500, parent_run_id=None, run_config=None, user_input: str = "",
                       prior_count: int = 0, current_count: int = 0, **kwargs):
    """只针对新增轮次输出 findings（不再传整个 previous_summary 让 LLM 重写）。

    prior_count: 已有摘要覆盖的轮次数（用于锚点）
    current_count: 已有消息触发的总轮次（用于提示 LLM 当前是第几轮）

    Cache-friendly: system prompt 完全静态，所有动态内容放在 user message。
    """
    new_count = max(current_count - prior_count, 1)

    # 动态内容全部拼到 user message，system prompt 保持不变以命中缓存
    user_parts = []
    if user_input:
        user_parts.append(f"用户原始问题：{user_input}")
        user_parts.append("请优先保留与用户问题直接相关的数据（股票代码、价格、涨跌幅、资金流向、板块名称），删除无关背景信息。")
    if prior_count > 0:
        user_parts.append(f"已有 R1..R{prior_count} 轮已被压缩并保留在历史摘要中，不要重写或重复它们。")
    else:
        user_parts.append("这是第一次压缩，没有历史压缩轮次。")
    user_parts.append(f"你的输出会被追加到历史摘要列表末尾，所以请只关注 R{prior_count + 1}..R{current_count} 这 {new_count} 轮。")
    user_parts.append(f"已有摘要覆盖到 R1..R{prior_count} 轮。\n")
    user_parts.append(f"本次新增轮次 R{prior_count + 1}..R{current_count}：\n" + new_rounds_text)
    user_parts.append(f"\n\n只输出这 {new_count} 轮的 findings（不要重写已有内容），总长度 ≤ {max_chars} 字符。")

    messages = [
        ("system", SUMMARY_SYSTEM_PROMPT),
        ("user", "\n".join(user_parts)),
    ]
    return llm_json_with_retry(
        llm, messages, logger, label="react-context-summary", budget=budget,
        metadata={"node": "react-context-summary"}, parent_run_id=parent_run_id, run_config=run_config,
        skip_cache_prefix=True,
    )


# ────────────────────────────────────────────────────────────────────
# Round 切分 + 摘要
# ────────────────────────────────────────────────────────────────────

@dataclass
class ReactRound:
    """一个 AI tool_calls + 后续 ToolMessage 组合的轮次。"""
    index: int
    ai_message: AIMessage
    tool_messages: list[ToolMessage]


def split_react_rounds(messages: list) -> list[ReactRound]:
    """从消息历史中切出 AI tool_calls + 后续 ToolMessage 轮次。"""
    rounds: list[ReactRound] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if not isinstance(msg, AIMessage) or not (msg.tool_calls or []):
            i += 1
            continue

        expected_ids = {
            tc.get("id") or tc.get("tool_call_id")
            for tc in msg.tool_calls
            if tc.get("id") or tc.get("tool_call_id")
        }
        tool_messages: list[ToolMessage] = []
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            tm = messages[j]
            if not expected_ids or tm.tool_call_id in expected_ids:
                tool_messages.append(tm)
                j += 1
                continue
            break

        rounds.append(ReactRound(index=len(rounds) + 1, ai_message=msg, tool_messages=tool_messages))
        i = j
    return rounds


def format_rounds_for_summary(rounds: Iterable[ReactRound]) -> str:
    """把 rounds 序列化成可发给 LLM summarizer 的文本。"""
    parts: list[str] = []
    for round_item in rounds:
        parts.append(f"Round {round_item.index}:")
        for tc in round_item.ai_message.tool_calls or []:
            parts.append(
                "tool_call "
                + json.dumps(
                    {
                        "name": tc.get("name"),
                        "args": tc.get("args") or {},
                        "id": tc.get("id") or tc.get("tool_call_id"),
                    },
                    ensure_ascii=False, sort_keys=True,
                )
            )
        for tm in round_item.tool_messages:
            parts.append(
                "tool_result "
                + json.dumps(
                    {
                        "name": tm.name,
                        "tool_call_id": tm.tool_call_id,
                        "content": str(tm.content or ""),
                    },
                    ensure_ascii=False, sort_keys=True,
                )
            )
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────
# ReactContextBuilder
# ────────────────────────────────────────────────────────────────────

class ReactContextBuilder(BaseMemory):
    """ReAct 历史轮次压缩器（继承 LangChain BaseMemory 以统一对外接口）。

    状态归属：本身无状态，所有 rounds / summary / summarized_until 都存在
    exec_state 上，由 LangGraph checkpoint 统一序列化/恢复。

    BaseMemory 4 方法语义映射：
        - memory_variables      → ["chat_history"]  注入到 chain inputs 的 key
        - load_memory_variables → 返回 {"chat_history": compact_messages}
        - save_context          → 触发 pending 轮次压缩（实际逻辑在 build）
        - clear                 → 自身无状态可清（exec_state 由调用方清）
    """

    # ──── BaseMemory 必需 property ────
    @property
    def memory_variables(self) -> list[str]:
        """注入到 chain inputs 的 key 名。"""
        return ["chat_history"]

    # ──── 字段声明（pydantic 兼容 BaseMemory） ────
    recent_rounds: int = 2
    summary_trigger_rounds: int = 2
    summary_pending_chars: int = 8000
    summary_max_chars: int = 2500
    enable_debug_log: bool = False
    summarizer: Optional[Callable] = None

    # ──── 构造 ────
    def __init__(
        self,
        recent_rounds: int = 2,
        summary_trigger_rounds: int = 2,
        summary_pending_chars: int = 8000,
        summary_max_chars: int = 2500,
        enable_debug_log: bool = False,
        summarizer: Optional[Callable] = None,
    ):
        # pydantic / BaseMemory 兼容：字段已在类级声明，super().__init__ 走 pydantic
        super().__init__(
            recent_rounds=max(int(recent_rounds), 0),
            summary_trigger_rounds=max(int(summary_trigger_rounds), 1),
            summary_pending_chars=max(int(summary_pending_chars), 1),
            summary_max_chars=max(int(summary_max_chars), 1),
            enable_debug_log=bool(enable_debug_log),
            summarizer=summarizer or default_summarizer,
        )

    # ──── 主入口（AgentNode 调用） ────
    def build(
        self,
        *,
        messages: list,
        user_input: str,
        exec_state,
        llm,
        logger,
        budget=None,
        parent_run_id=None,
        run_config=None,
    ) -> list:
        """构造 compact 视图：system + user + (summary) + recent_rounds。

        流程：
        1. 拆出 AI+Tool 轮次
        2. 区分 pending（待压缩）和 recent（保留原样）
        3. 满足触发条件 → 调 LLM summarizer → 写回 exec_state
        4. 拼装 compact（summary 放原位置）
        """
        logger = ensure_radar(logger)
        rounds = split_react_rounds(messages)
        recent = rounds[-self.recent_rounds:] if self.recent_rounds else []
        recent_start = recent[0].index if recent else len(rounds) + 1
        summarized_until = int(getattr(exec_state, "react_context_summarized_until", 0) or 0)
        pending = [r for r in rounds if summarized_until < r.index < recent_start]

        self._maybe_summarize(
            pending, exec_state, llm, logger, budget,
            parent_run_id=parent_run_id, run_config=run_config, user_input=user_input,
        )
        # 如果 summary 失败，pending 轮次需要原样保留
        # 检查 summarized_until 是否推进了
        new_summarized_until = int(getattr(exec_state, "react_context_summarized_until", 0) or 0)
        unsent_pending = [r for r in pending if r.index > new_summarized_until]
        compact = self._compose(messages, rounds, recent, user_input, exec_state, pending=unsent_pending)

        if self.enable_debug_log:
            try:
                summaries = getattr(exec_state, "react_context_summaries", None) or []
                summary_chars = sum(
                    len(json.dumps(s, ensure_ascii=False, sort_keys=True))
                    for s in summaries
                )
                logger.info(
                    "R",
                    "react.context.compact "
                    f"before_messages={len(messages)} after_messages={len(compact)} "
                    f"rounds={len(rounds)} recent_rounds={len(recent)} "
                    f"summaries={len(summaries)} summary_chars={summary_chars}",
                )
            except Exception:
                pass
        return compact

    # ──── 摘要触发 ────
    def _maybe_summarize(self, pending, exec_state, llm, logger, budget, **kwargs) -> None:
        """触发 LLM summarizer，把结果追加到 summaries 列表（不覆盖历史摘要）。

        关键：每次只压缩"上次摘要之后的"pending 轮次，避免重复处理已压缩内容。
        """
        if not pending:
            return
        pending_text = format_rounds_for_summary(pending)
        should_summarize = (
            len(pending) >= self.summary_trigger_rounds
            or len(pending_text) >= self.summary_pending_chars
        )
        if not should_summarize:
            return

        existing_summaries = list(getattr(exec_state, "react_context_summaries", None) or [])
        prior_count = sum(
            len(s.get("summarized_rounds") or []) for s in existing_summaries
        )
        current_count = pending[-1].index

        try:
            result = self.summarizer(
                None,  # 不再传 previous_summary（避免 LLM 重复处理）
                pending_text, llm=llm, logger=logger, budget=budget,
                max_chars=self.summary_max_chars,
                prior_count=prior_count, current_count=current_count, **kwargs,
            )
        except Exception as exc:
            ensure_radar(logger).warning("R", f"react.context.summary failed: {exc}")
            return

        if not isinstance(result, dict):
            ensure_radar(logger).warning("R", "react.context.summary invalid_json")
            return

        result.setdefault("summary_version", len(existing_summaries) + 1)
        result.setdefault("summarized_rounds", [r.index for r in pending])
        result.setdefault("rounds_summary", "")
        result.setdefault("key_findings", "")
        result.setdefault("data_coverage", [])
        result.setdefault("failed_queries", [])

        # 归一化 summarized_rounds：LLM 可能返回字符串 "R3"、"R3-R4"、"3-4" 等格式
        # 统一提取为整数列表，避免 _summary_to_message 再加 R 前缀时变成 RR
        result["summarized_rounds"] = _normalize_rounds(result["summarized_rounds"])

        existing_summaries.append(result)
        exec_state.react_context_summaries = existing_summaries
        exec_state.react_context_summarized_until = pending[-1].index
        exec_state.react_context_summary_updates = int(
            getattr(exec_state, "react_context_summary_updates", 0) or 0
        ) + 1

    # ──── 视图拼装 ────
    def _compose(self, messages, rounds, recent, user_input, exec_state, pending=None) -> list:
        """摘要放原位置：system + user + (所有历史 summaries) + pending_rounds + recent_rounds。

        关键：每个压缩窗口的 summary 单独保留为 1 个 AIMessage，按时间顺序追加。
        如果 summary 失败，pending 轮次原样保留，避免丢失数据。
        """
        # 收集头部连续 system 消息（SubAgent 场景有多条，主 ReAct 只有一条）
        system_parts = []
        for m in messages:
            if _role(m) == "system":
                system_parts.append(m[1] if isinstance(m, tuple) else str(getattr(m, "content", "")))
            else:
                break
        system_msg = ("system", "\n\n".join(system_parts)) if system_parts else ("system", "")
        compact: list = [system_msg, ("user", user_input)]

        summaries = list(getattr(exec_state, "react_context_summaries", None) or [])
        for summary in summaries:
            compact.append(self._summary_to_message(summary))

        # 如果有未压缩的 pending 轮次（summary 失败），原样保留
        if pending:
            for round_item in pending:
                compact.append(round_item.ai_message)
                compact.extend(round_item.tool_messages)

        for round_item in recent:
            compact.append(round_item.ai_message)
            compact.extend(round_item.tool_messages)
        return compact

    @staticmethod
    def _summary_to_message(summary: dict) -> AIMessage:
        version = summary.get("summary_version", "?")
        rounds = summary.get("summarized_rounds") or []
        rounds_label = (
            f"R{rounds[0]}..R{rounds[-1]}" if rounds and len(rounds) > 1
            else (f"R{rounds[0]}" if rounds else "")
        )
        return AIMessage(
            content=(
                f"【已压缩历史轮次摘要 #{version}】{rounds_label}：\n"
                "以下是已成功获取的数据，key_findings 和 data_coverage 中的数据可靠，无需重新查询。"
                "failed_queries 仅表示该特定查询未成功，不影响其他已获取数据。\n"
                "```json\n"
                + json.dumps(summary, ensure_ascii=False)
                + "\n```"
            )
        )

    # ──── BaseMemory 接口实现 ────
    def load_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """返回 {"chat_history": compact_messages}。

        inputs 必含: messages, user_input, exec_state
        inputs 可选: llm, logger, budget, parent_run_id, run_config
        """
        compact = self.build(
            messages=inputs["messages"],
            user_input=inputs["user_input"],
            exec_state=inputs["exec_state"],
            llm=inputs.get("llm"),
            logger=inputs.get("logger"),
            budget=inputs.get("budget"),
            parent_run_id=inputs.get("parent_run_id"),
            run_config=inputs.get("run_config"),
        )
        return {"chat_history": compact}

    def save_context(self, inputs: dict[str, Any], outputs: dict[str, Any]) -> None:
        """BaseMemory 接口：保存本轮上下文。

        实际触发逻辑在 build() 里（边压缩边返回）。
        这里保持 no-op，让 load_memory_variables 调用方决定何时触发。
        """
        return None

    def clear(self) -> None:
        """BaseMemory 接口：清空 memory 内容。

        自身无状态，exec_state 由调用方清空。
        """
        return None

    # BaseMemory 提供 async 默认实现，复用 sync 版本即可
    async def aload_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return self.load_memory_variables(inputs)

    async def asave_context(self, inputs: dict[str, Any], outputs: dict[str, Any]) -> None:
        return self.save_context(inputs, outputs)

    async def aclear(self) -> None:
        self.clear()


def _normalize_rounds(rounds) -> list[int]:
    """归一化 summarized_rounds：从各种 LLM 返回格式中提取整数列表。

    支持格式：[1, 2], ["R3", "R4"], "R3-R4", "3-4", [3, "R4"], "R9-R10" 等。
    """
    if not rounds:
        return []
    if isinstance(rounds, str):
        # "R9-R10" 或 "3-4" 或 "R9, R10" 等
        parts = re.split(r'[,;\-–]+', rounds)
        rounds = parts
    result = []
    for item in rounds:
        if isinstance(item, (int, float)):
            result.append(int(item))
        elif isinstance(item, str):
            # 提取字符串中的数字
            nums = re.findall(r'\d+', item)
            for n in nums:
                result.append(int(n))
    return sorted(set(result))


def _role(msg) -> str:
    if isinstance(msg, tuple) and len(msg) == 2:
        return str(msg[0])
    return str(getattr(msg, "type", msg.__class__.__name__))

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

from config import Config
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


def _summary_llm(fallback=None):
    """上下文压缩专用 LLM（PRD-A11 FR-2：已与工具输出压缩拆开）。

    **为什么拆**：fork 形态要求压缩调用与 agent 走**同一 provider**——prompt cache
    按 provider 隔离，跨端点前缀恒不命中。因此 fork 档必须用主模型，
    而工具输出压缩本期不动、继续享受 sensenova 免费额度。两者曾共用
    ``COMPRESS_LLM_*``，无法同时表达"一个要同源、一个要免费"。

    优先级：``CONTEXT_SUMMARY_LLM_*`` → （standalone 档回落）``COMPRESS_LLM_*`` → 主模型。

    取不到时静默回落调用方传入的 llm，绝不让压缩链路因拿模型而崩。
    """
    if getattr(Config, "CONTEXT_SUMMARY_MODE", "standalone") == "fork":
        # fork 档：必须与 agent 同 provider，否则前缀缓存不共享（设计稿 §2.4）
        return fallback
    try:
        from utils.llm_factory import get_compress_llm
        return get_compress_llm() or fallback
    except Exception:
        return fallback


def default_summarizer(previous_summary, new_rounds_text: str, *, llm, logger, budget=None,
                       max_chars: int = 2500, parent_run_id=None, run_config=None, user_input: str = "",
                       prior_count: int = 0, current_count: int = 0, **kwargs):
    """只针对新增轮次输出 findings（不再传整个 previous_summary 让 LLM 重写）。

    prior_count: 已有摘要覆盖的轮次数（用于锚点）
    current_count: 已有消息触发的总轮次（用于提示 LLM 当前是第几轮）

    Cache-friendly: system prompt 完全静态，所有动态内容放在 user message。

    LLM 选择：standalone 档走 CONTEXT_SUMMARY_LLM_*（回落 COMPRESS_LLM_*）；
    fork 档直接用传入的 llm（主模型，见 _summary_llm —— 跨 provider 缓存不共享）。

    **fork 形态**（PRD-A11 FR-2）：``fork_prefix`` 非空时，压缩输入 = agent 已发过的
    完整对话前缀 + 尾部压缩指令。pending 全文本来就在前缀里，**不再重复发一遍**，
    尾部只保留「归档哪些轮次」这类锚点信息。
    """
    fork_prefix = kwargs.get("fork_prefix")
    llm = _summary_llm(llm)
    new_count = max(current_count - prior_count, 1)

    # 锚点信息：fork 与 standalone 共用同一套措辞，保证摘要质量不因形态而变
    anchor_parts = []
    if user_input:
        anchor_parts.append(f"用户原始问题：{user_input}")
        anchor_parts.append("请优先保留与用户问题直接相关的数据（股票代码、价格、涨跌幅、资金流向、板块名称），删除无关背景信息。")
    if prior_count > 0:
        anchor_parts.append(f"已有 R1..R{prior_count} 轮已被压缩并保留在历史摘要中，不要重写或重复它们。")
    else:
        anchor_parts.append("这是第一次压缩，没有历史压缩轮次。")
    anchor_parts.append(f"你的输出会被追加到历史摘要列表末尾，所以请只关注 R{prior_count + 1}..R{current_count} 这 {new_count} 轮。")

    if fork_prefix is not None:
        # fork：指令走尾部，pending 全文已在前缀里（设计稿 §2.4）
        from agents.shared.llm_fork import build_fork_payload, fork_metadata
        tail = (
            f"【上下文归档】本次要归档的轮次：R{prior_count + 1}..R{current_count}"
            f"（共 {new_count} 轮，它们的工具调用与返回结果就在上面这段对话里）。\n"
            + "\n".join(anchor_parts)
            + f"\n\n只输出这 {new_count} 轮的 findings（不要重写已有内容），"
              f"总长度 ≤ {max_chars} 字符。字段：summary_version / summarized_rounds / "
              f"rounds_summary / key_findings / data_coverage / failed_queries。"
        )
        payload = build_fork_payload(fork_prefix, tail)
        if payload is not None:
            messages, tools = payload
            try:
                llm_with_tools = llm.bind_tools(tools)
            except Exception:      # 绑定失败不阻断：前缀仍能命中，只是不具备工具调用能力
                llm_with_tools = llm
            return llm_json_with_retry(
                llm_with_tools, messages, logger, label="react-context-summary",
                budget=budget,
                metadata={"node": "react-context-summary",
                          "compress_reason": kwargs.get("compress_reason", ""),
                          **fork_metadata(fork_prefix, payload)},
                parent_run_id=parent_run_id, run_config=run_config,
            )
        # fork_prefix 不可用（无 tools 等）→ 落回 standalone

    # standalone：system 静态 + 动态全在 user（保持既有形态）
    user_parts = list(anchor_parts)
    user_parts.append(f"已有摘要覆盖到 R1..R{prior_count} 轮。\n")
    user_parts.append(f"本次新增轮次 R{prior_count + 1}..R{current_count}：\n" + new_rounds_text)
    user_parts.append(f"\n\n只输出这 {new_count} 轮的 findings（不要重写已有内容），总长度 ≤ {max_chars} 字符。")

    messages = [
        ("system", SUMMARY_SYSTEM_PROMPT),
        ("user", "\n".join(user_parts)),
    ]
    return llm_json_with_retry(
        llm, messages, logger, label="react-context-summary", budget=budget,
        metadata={"node": "react-context-summary", "fork_kind": "standalone",
                  "fork_skip_reason": "fork_disabled",
                  "compress_reason": kwargs.get("compress_reason", "")},
        parent_run_id=parent_run_id, run_config=run_config,
        skip_cache_prefix=True,
    )


# ────────────────────────────────────────────────────────────────────
# Round 切分 + 摘要
# ────────────────────────────────────────────────────────────────────

def _message_text(m) -> str:
    """取消息的文本内容（兼容 LangChain 消息对象与 (role, content) 元组）。"""
    content = getattr(m, "content", None)
    if content is None and isinstance(m, (tuple, list)) and len(m) > 1:
        content = m[1]
    if isinstance(content, list):      # 多模态/结构化 content
        content = "".join(str(c.get("text") or c) if isinstance(c, dict) else str(c)
                          for c in content)
    return str(content or "")


def _estimate_payload_tokens(messages: Iterable) -> int:
    """估算一次 LLM 调用的 payload 体量（token）。

    口径与项目既有「3 字符/token」一致，不额外调 tokenizer —— 这个函数每轮
    都会调用一次（`_maybe_summarize` 前），必须廉价。

    ⚠️ 覆盖**整个 messages**而非只算工具轮次：软闸门要回答的是「这次请求会不会
    撑爆上下文」，而 tools 数组、agent system、guidance、user 加起来约 10K tok，
    只算轮次会让阈值语义与直觉差出一大截。
    """
    total = 0
    for m in messages or []:
        total += len(_message_text(m))
        for tc in (getattr(m, "tool_calls", None) or []):
            try:
                total += len(json.dumps(tc.get("args") or {}, ensure_ascii=False))
            except Exception:
                pass
    return total // 3

@dataclass
class ReactRound:
    """一个 AI tool_calls + 后续 ToolMessage 组合的轮次。"""
    index: int
    ai_message: AIMessage
    tool_messages: list[ToolMessage]


def split_react_rounds(messages: list) -> list[ReactRound]:
    """从消息历史中切出 AI tool_calls + 后续 ToolMessage 轮次。"""
    rounds, _ = split_react_rounds_with_spans(messages)
    return rounds


def split_react_rounds_with_spans(messages: list) -> tuple[list[ReactRound], list[tuple[int, int, int]]]:
    """同 split_react_rounds，额外返回每个轮次在 messages 中的下标区间。

    spans: [(start, end, round_index)]，end 为开区间。供 _compose 做「原位替换」——
    必须按下标遍历：BaseMessage 未冻结、不可 hash，不能拿消息对象当字典键。
    """
    rounds: list[ReactRound] = []
    spans: list[tuple[int, int, int]] = []
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
        spans.append((i, j, len(rounds)))
        i = j
    return rounds, spans


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
        tools=None,
    ) -> list:
        """构造 compact 视图：原位替换轮次，其余消息按原序透传。

        流程：
        1. 拆出 AI+Tool 轮次（含下标区间）
        2. 区分 pending（待压缩）和 recent（保留原样）
        3. 满足触发条件 → 调 LLM summarizer → 写回 exec_state
        4. 原位替换：只删除「确有摘要覆盖」的轮次，摘要插在它首条消息的位置

        不变量：任何非轮次消息（instruction / 历史 / guidance / 约束）都不允许被丢弃。
        """
        logger = ensure_radar(logger)
        rounds, spans = split_react_rounds_with_spans(messages)
        recent = rounds[-self.recent_rounds:] if self.recent_rounds else []
        recent_start = recent[0].index if recent else len(rounds) + 1
        summarized_until = int(getattr(exec_state, "react_context_summarized_until", 0) or 0)
        pending = [r for r in rounds if summarized_until < r.index < recent_start]

        # 软闸门用的 payload 体量估算：口径与项目既有 3 字符/token 一致，
        # 不额外调 tokenizer（这里每轮都会跑一次，开销敏感）。
        # ⚠️ 必须算**整个 messages**，不能只算 rounds —— 实测两者差 ~10K
        # （tools 数组 + agent system + guidance + user），只算 rounds 会让
        # REACT_CONTEXT_SOFT_LIMIT_TOKENS 的语义与"上下文快满了"这个直觉对不上。
        self._maybe_summarize(
            pending, exec_state, llm, logger, budget,
            parent_run_id=parent_run_id, run_config=run_config, user_input=user_input,
            est_payload_tokens=_estimate_payload_tokens(messages),
            tools=tools, messages=messages,
        )
        # 摘要失败时 _maybe_summarize 不会推进游标；未被摘要覆盖的轮次在 _compose 中自动原样保留
        compact, dropped_non_round = self._compose(
            messages, rounds, spans, exec_state, logger=logger,
        )

        if self.enable_debug_log:
            try:
                summaries = getattr(exec_state, "react_context_summaries", None) or []
                summary_chars = sum(
                    len(json.dumps(s, ensure_ascii=False, sort_keys=True))
                    for s in summaries
                )
                compressed_rounds = sum(
                    1 for r in rounds if _summary_covering(r.index, summaries) is not None
                )
                logger.info(
                    "R",
                    "react.context.compact "
                    f"before_messages={len(messages)} after_messages={len(compact)} "
                    f"rounds={len(rounds)} recent_rounds={len(recent)} "
                    f"compressed_rounds={compressed_rounds} dropped_non_round={dropped_non_round} "
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

        reasons: list[str] = []
        # ① 软闸门优先：预估本次 payload 超过软上限 → 无条件压缩（PRD-A11 FR-4）
        est_tokens = kwargs.get("est_payload_tokens")
        soft_limit = int(getattr(Config, "REACT_CONTEXT_SOFT_LIMIT_TOKENS", 0) or 0)
        if est_tokens and soft_limit and est_tokens > soft_limit:
            reasons.append("soft_limit")
        # ② 轮次 / 字符阈值
        if len(pending) >= self.summary_trigger_rounds:
            reasons.append("rounds")
        if len(pending_text) >= self.summary_pending_chars:
            reasons.append("chars")
        if not reasons:
            return
        compress_reason = ",".join(reasons)
        logger.debug("R", f"react.context.compress reason={compress_reason} "
                          f"pending={len(pending)} rounds chars={len(pending_text)}"
                          + (f" est_tokens={est_tokens}" if est_tokens else ""))

        existing_summaries = list(getattr(exec_state, "react_context_summaries", None) or [])
        prior_count = sum(
            len(s.get("summarized_rounds") or []) for s in existing_summaries
        )
        current_count = pending[-1].index

        # fork 档：把「本次迭代即将发出的完整 messages + tools」交给 summarizer。
        # ⚠️ 采集必须发生在压缩**之后**、agent 调用之前 —— 压缩前 last_llm_* 是上一轮
        # 发的 payload（不含本轮新工具结果），那正是我们要复用的热前缀。
        fork_prefix = None
        # ⚠️ tools 走 **kwargs 进来（build() 的形参），不能直接引用
        _tools = kwargs.get("tools")
        if getattr(Config, "CONTEXT_SUMMARY_MODE", "standalone") == "fork" and _tools:
            # ⚠️ 必须按「agent 实际 payload」的骨架快照：agent 发出的 messages[0] 是
            # merge_leading_system 合并后的单条（`[REACT]\n\n[guidance]`），而本函数入参
            # kwargs["messages"] 是**未合并**的原始 list（`[REACT]`+`[guidance]` 两条）。
            # 不合并 → 快照 messages[0]=1,544 字符（裸 REACT），与 agent 的 3,302 不同族 →
            # 压缩首调只能命中 tools 块（实测 4,096），拿不到 agent 已写热的 system 前缀。
            from agents.shared.prompt_prefix import make_fork_prefix, merge_leading_system
            fork_prefix = make_fork_prefix(
                merge_leading_system(list(kwargs.get("messages") or [])),
                _tools, source="context_summary")

        try:
            result = self.summarizer(
                None,  # 不再传 previous_summary（避免 LLM 重复处理）
                pending_text, llm=llm, logger=logger, budget=budget,
                max_chars=self.summary_max_chars,
                prior_count=prior_count, current_count=current_count,
                fork_prefix=fork_prefix, compress_reason=compress_reason, **kwargs,
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

        # 记录本次摘要的「覆盖窗口」= 实际送进 summarizer 的 pending 区间。
        # _compose 用它判定轮次能否被替换（比 LLM 自述的 summarized_rounds 可靠）
        result["covered_from"] = pending[0].index
        result["covered_until"] = pending[-1].index

        existing_summaries.append(result)
        exec_state.react_context_summaries = existing_summaries
        exec_state.react_context_summarized_until = pending[-1].index
        exec_state.react_context_summary_updates = int(
            getattr(exec_state, "react_context_summary_updates", 0) or 0
        ) + 1
        result["compress_reason"] = compress_reason

    # ──── 视图拼装 ────
    def _compose(self, messages, rounds, spans, exec_state, logger=None) -> tuple[list, int]:
        """原位替换：只删除「落在某次成功摘要覆盖窗口内」的轮次，其余消息按原序透传。

        不变量：压缩器只能压自己产生的 tool 轮次（AI tool_calls + 其 ToolMessage）。
        非轮次消息（planner 的 instruction、对话历史、模板 guidance、[用户约束]）
        一律按原序保留——结构上不存在「丢失」的路径。

        返回 (compact, dropped_non_round)；dropped_non_round 正常恒为 0，
        非 0 说明出现回归，立即打 WARNING 暴露。

        注：历史签名的 recent / user_input / pending 已不再参与拼装
        （压缩与否只看摘要窗口，见 _summary_covering），仅保留以兼容调用方。
        """
        summaries = list(getattr(exec_state, "react_context_summaries", None) or [])
        compact: list = []
        emitted_summary_ids: set[int] = set()

        # 已被摘要覆盖的轮次里的 tool_call_id：用于跳过「游离的 ToolMessage」
        # （协议异常时 ToolMessage 可能不在其 AI 消息之后，留下会变成无主 tool_call_id）
        compressed_call_ids: set[str] = set()
        for start, end, round_index in spans:
            if _summary_covering(round_index, summaries) is None:
                continue
            for msg in messages[start:end]:
                if isinstance(msg, ToolMessage) and msg.tool_call_id:
                    compressed_call_ids.add(msg.tool_call_id)

        def _extend_passthrough(chunk):
            for msg in chunk:
                if isinstance(msg, ToolMessage) and msg.tool_call_id in compressed_call_ids:
                    continue
                compact.append(msg)

        pos = 0
        for start, end, round_index in spans:
            # ① 轮次之前的非轮次消息：原序透传
            _extend_passthrough(messages[pos:start])

            summary = _summary_covering(round_index, summaries)
            if summary is None:
                # ② 未被摘要覆盖（含摘要失败回退）→ 整轮原样保留，tool 协议完整
                compact.extend(messages[start:end])
            else:
                # ③ 已被摘要覆盖 → 摘要插在「该轮次首条位置」，轮次整体删除
                key = id(summary)
                if key not in emitted_summary_ids:
                    compact.append(self._summary_to_message(summary))
                    emitted_summary_ids.add(key)
            pos = end

        # ④ 末尾的非轮次消息：原序透传
        _extend_passthrough(messages[pos:])

        # 守卫必须在合并头部 system 之前（合并会新建对象，破坏身份比对）
        dropped_non_round = _count_dropped_non_round(messages, spans, compact)
        if dropped_non_round:
            ensure_radar(logger).warning(
                "R",
                f"react.context.drop_non_round n={dropped_non_round} "
                f"before={len(messages)} after={len(compact)} "
                "—— 非轮次消息被丢弃，违反 step 信息不变量，请排查 _compose",
            )

        # D1-B：保留头部连续 system 合并（维持与旧实现一致的 prompt 缓存前缀形态）
        return _merge_leading_system(compact), dropped_non_round

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


def _content(msg) -> str:
    if isinstance(msg, tuple) and len(msg) == 2:
        return str(msg[1])
    return str(getattr(msg, "content", ""))


def _summary_covering(round_index: int, summaries: list) -> Optional[dict]:
    """返回覆盖该轮次的摘要。

    覆盖判定以「摘要窗口」为准：窗口 = 该次送进 summarizer 的 pending 区间
    （由 _maybe_summarize 记录为 covered_from/covered_until）。
    理由：
    - LLM 可能少报/空报 summarized_rounds（实测有返回 [] 的情况），若只信它的自述，
      压缩会永久失效（原文一直堆在上下文里）；若只信游标，又可能出现「游标推进了
      但该轮次从未进过 summarizer」的无凭据删除。窗口恰好两者兼顾。
    - 兼容旧 checkpoint：无窗口字段时退回用 summarized_rounds 的 min/max。
    """
    for summary in summaries:
        start = summary.get("covered_from")
        end = summary.get("covered_until")
        if start is not None and end is not None:
            if int(start) <= round_index <= int(end):
                return summary
            continue
        rounds = summary.get("summarized_rounds") or []
        if rounds and min(rounds) <= round_index <= max(rounds):
            return summary
    return None


def _merge_leading_system(messages: list) -> list:
    """把头部连续 system 消息合并为一条（保持与旧实现一致的缓存前缀形态）。

    ⚠️ 实现已上提到 :func:`agents.shared.prompt_prefix.merge_leading_system` ——
    **单一事实源**。本函数只是别名：主前缀构造器（select_skills / skill-router /
    planner）与压缩链路必须产出**逐字节相同**的消息骨架；两处各写一份必然分叉
    （2026-10-16 实测：agent 侧合并成一条 3118 字符、select_skills 侧保留两条，
    前缀从第 2 条消息就废了，agent#1 只命中 50%）。
    """
    from agents.shared.prompt_prefix import merge_leading_system
    return merge_leading_system(messages)


def _count_dropped_non_round(messages: list, spans: list[tuple[int, int, int]], compact: list) -> int:
    """统计「非轮次消息」中有多少条没有进入 compact（正常应恒为 0）。

    只统计真正的上下文消息（system / human / 文本 AI）——ToolMessage 属于 tool 协议
    产物，不作为 step 信息统计，避免协议边界情况造成误报。

    按对象身份比对：compact 中的消息都是原对象引用，仅头部 system 合并会新建对象，
    因此本函数必须在合并之前调用。
    """
    in_round = set()
    for start, end, _ in spans:
        in_round.update(range(start, end))
    kept_ids = {id(m) for m in compact}
    return sum(
        1 for i, m in enumerate(messages)
        if i not in in_round
        and not isinstance(m, ToolMessage)
        and id(m) not in kept_ids
    )

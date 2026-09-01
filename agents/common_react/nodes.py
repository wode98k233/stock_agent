"""子 ReAct 图节点

核心设计（与主 ReAct 节点对齐，适配单步执行场景）：
- SubAgentNode: 继承 BaseAgentNode，通过 _prepare_messages 钩子注入去重和上下文压缩
- build_sub_tool_node: 工具执行节点，支持并行执行、同迭代去重、限流、输出精简/压缩
- should_continue: 条件路由，检查 should_stop / 迭代上限 / tool_calls
- extract_final_result: 从状态中提取最终结果

非序列化对象（budget/exec_state/logger/tools）通过节点 __init__ 闭包捕获，
不存入 ReactSubState，确保 state 可被 LangGraph checkpoint 序列化。
"""
from typing import Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from agents.common_react.state import ReactSubState
from agents.react.nodes import BaseAgentNode
from agents.react.tool_node_base import BaseToolNode
from agents.react.context import ReactContextBuilder
from agents.react.utils import (
    PerToolRateLimiter,
    _collect_executed_tool_sigs,
    _compress_tool_output,
)
from agents.executor_callbacks import ExecutionState
from config import Config


class SubAgentNode(BaseAgentNode):
    """子 ReAct LLM Agent 节点（继承 BaseAgentNode）

    差异点：
    - _get_user_goal: 使用 step_purpose 替代 user_input
    - _prepare_messages: 迭代间去重（工具签名）+ 上下文压缩
    """

    def _get_user_goal(self, state: dict) -> str:
        return state.get("step_purpose", "")

    def _prepare_messages(self, messages: list, state: dict, config, force_summary_msg: str | None) -> list:
        exec_state = self.exec_state
        logger = self.logger
        budget = self.budget

        # 迭代间去重
        if state["iteration_count"] >= 1:
            already_done = _collect_executed_tool_sigs(messages)
            if already_done:
                dedup_hint = (
                    "【系统提示】以下工具调用已在之前执行过，结果已在上下文中，"
                    "请勿重复调用。如需更多信息，请用不同的查询条件。\n"
                    + "\n".join(f"- {sig}" for sig in sorted(already_done))
                )
                messages = list(messages) + [("system", dedup_hint)]

            # 同工具名软引导
            tool_call_counts = {}
            for msg in messages:
                if isinstance(msg, AIMessage) and msg.tool_calls:
                    for tc in msg.tool_calls:
                        name = tc.get("name", "")
                        if name:
                            tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
            if tool_call_counts:
                count_desc = "、".join(f"{n}({c}次)" for n, c in sorted(tool_call_counts.items()))
                soft_hint = (
                    "【系统提示】已调用工具：{counts}。"
                    "结果已在上下文中。如果已获取到任务所需的数据，**立刻停止调用工具并生成总结**。"
                    "不要用不同表述重新查询同一类数据。"
                ).format(counts=count_desc)
                messages = list(messages) + [("system", soft_hint)]

        # 上下文压缩（复用主 ReAct 能力）
        if Config.REACT_ENABLE_CONTEXT_COMPACTION:
            context_builder = ReactContextBuilder(
                recent_rounds=Config.REACT_CONTEXT_RECENT_ROUNDS,
                summary_trigger_rounds=Config.REACT_SUMMARY_TRIGGER_ROUNDS,
                summary_pending_chars=Config.REACT_SUMMARY_PENDING_CHARS,
                summary_max_chars=Config.REACT_SUMMARY_MAX_CHARS,
                enable_debug_log=Config.REACT_CONTEXT_DEBUG_LOG,
            )
            parent_run_id = config.get("run_id") if config else None
            messages = context_builder.build(
                messages=list(messages),
                user_input=self._get_user_goal(state),
                exec_state=exec_state,
                llm=self.llm,
                logger=logger,
                budget=budget,
                parent_run_id=parent_run_id,
                run_config=config,
            )

        return messages


class SubToolNode(BaseToolNode):
    """子 ReAct 工具节点，复用基础工具执行流程，并注入 step 目标用于输出压缩。"""

    def __init__(
        self,
        exec_state=None,
        tools=None,
        logger=None,
        rate_limiter: Optional[PerToolRateLimiter] = None,
        user_goal_ref: dict = None,
    ):
        super().__init__(exec_state=exec_state, tools=tools, logger=logger, rate_limiter=rate_limiter)
        self._user_goal_ref = user_goal_ref or {"goal": ""}

    def get_config(self):
        return Config

    async def compress_tool_output(self, tool_name: str, content: str) -> str:
        return await _compress_tool_output(
            tool_name,
            content,
            self._logger,
            user_goal=self._user_goal_ref.get("goal", ""),
        )


def build_sub_tool_node(exec_state=None, tools=None, logger=None, rate_limiter: Optional[PerToolRateLimiter] = None, user_goal_ref: dict = None):
    """构建子 ReAct ToolNode，支持并行执行、去重、限流、压缩

    与主 ReAct 的 build_tool_node 对齐，但操作 ReactSubState。
    exec_state/tools/logger 通过闭包捕获，不从 state 读取。
    user_goal_ref: 可变 dict {"goal": str}，ainvoke 时更新，压缩时读取。
    """
    return SubToolNode(
        exec_state=exec_state,
        tools=tools,
        logger=logger,
        rate_limiter=rate_limiter,
        user_goal_ref=user_goal_ref,
    )


def should_continue(state: ReactSubState) -> str:
    """条件路由：决定下一步走向

    返回:
        "tools" -> 继续执行工具（LLM 返回了 tool_calls）
        "end"   -> 结束（达到限制 / 无 tool_calls / 强制停止）
    """
    if state.get("should_stop"):
        return "end"
    if state["iteration_count"] >= state["max_iterations"]:
        return "end"
    messages = state["messages"]
    last_msg = messages[-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "end"


def extract_final_result(state: ReactSubState) -> Optional[str]:
    """从状态中提取最终结果（最后一条 AI 文本消息）"""
    from agents.react.nodes import _has_tool_call_xml, _TOOL_CALL_XML_RE
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            content = msg.content
            # 清理残留的 XML tool_call 文本（LLM 偶尔会混入回复中）
            if _has_tool_call_xml(content):
                content = _TOOL_CALL_XML_RE.sub("", content).strip()
            return content if content else None
    return None

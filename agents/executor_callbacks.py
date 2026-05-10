"""
选股雷达 - 执行器回调模块
用于捕获 React Agent 的中间状态，即使达到递归限制也能提取已有结果
同时负责 tool 指标采集和调试日志（合并原 MetricsCallback + AgentDebugCallback）
"""
from typing import List, Dict, Any, Optional
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from config import Config
from utils.logger import RequestContext, ensure_radar


class ToolCall:
    """工具调用记录"""
    def __init__(self, tool_name: str, tool_input: str, tool_output: Optional[str] = None):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.tool_output = tool_output
        self.success = tool_output is not None and "执行失败" not in str(tool_output)


class ExecutionState:
    """执行状态捕获器"""
    def __init__(self):
        self.messages: List[BaseMessage] = []
        self.tool_calls: List[ToolCall] = []
        self.current_tool_name: Optional[str] = None
        self.current_tool_input: Optional[str] = None
        self.iteration_count = 0
        self.max_iterations_reached = False

    def reset(self):
        self.messages = []
        self.tool_calls = []
        self.current_tool_name = None
        self.current_tool_input = None
        self.iteration_count = 0
        self.max_iterations_reached = False

    def add_message(self, message: BaseMessage):
        self.messages.append(message)

    def start_tool(self, tool_name: str, tool_input: str):
        self.current_tool_name = tool_name
        self.current_tool_input = tool_input

    def end_tool(self, tool_output: str):
        if self.current_tool_name and self.current_tool_input:
            tool_call = ToolCall(
                tool_name=self.current_tool_name,
                tool_input=self.current_tool_input,
                tool_output=tool_output
            )
            self.tool_calls.append(tool_call)
            self.current_tool_name = None
            self.current_tool_input = None

    def increment_iteration(self):
        self.iteration_count += 1

    def get_summary_context(self) -> str:
        parts = []

        if self.tool_calls:
            parts.append("已执行的工具调用：")
            for i, call in enumerate(self.tool_calls, 1):
                status = "✅" if call.success else "❌"
                parts.append(f"\n{status} 工具 {i}: {call.tool_name}")
                parts.append(f"   输入: {call.tool_input[:200]}...")
                if call.tool_output:
                    parts.append(f"   输出: {call.tool_output[:500]}...")

        last_ai_msg = None
        for msg in reversed(self.messages):
            if isinstance(msg, AIMessage):
                last_ai_msg = msg
                break

        if last_ai_msg:
            parts.append(f"\n\n最后的分析结果：\n{last_ai_msg.content[:1000]}...")

        if self.max_iterations_reached:
            parts.append(f"\n\n⚠️ 注意：已达到最大迭代次数 ({Config.REACT_TOOL_CALLS})，部分目标可能未完成。")

        return "\n".join(parts) if parts else "暂无可用结果"

    def has_useful_results(self) -> bool:
        return len(self.tool_calls) > 0 or len(self.messages) > 2


class ExecutionStateCallback(BaseCallbackHandler):
    """执行状态 + tool 指标采集 + 调试日志（三位一体）"""

    def __init__(self, state: ExecutionState, logger):
        self.state = state
        self.logger = ensure_radar(logger)
        self.is_debug = Config.LOG_LEVEL == 'DEBUG'

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        if self.is_debug:
            self.logger.debug("X", "链开始")

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        if self.is_debug:
            self.logger.debug("X", "链结束")

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self.state.increment_iteration()
        if self.is_debug:
            self.logger.debug("X", f"LLM 调用 #{self.state.iteration_count}")

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        if response.generations and len(response.generations) > 0:
            for gen in response.generations[0]:
                if hasattr(gen, 'text'):
                    msg = AIMessage(content=gen.text)
                    self.state.add_message(msg)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        tool_name = serialized.get('name', 'unknown')
        self.state.start_tool(tool_name, input_str)
        if self.is_debug:
            self.logger.debug("T", f"→ 工具开始: {tool_name}", input=str(input_str)[:500])

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        output_str = str(output)
        self.state.end_tool(output_str)
        try:
            ctx = RequestContext.current()
            if ctx:
                ctx.record_tool()
        except Exception:
            pass
        if self.is_debug:
            self.logger.debug("T", "← 工具完成", output_len=len(output_str), output=output_str[:1000])

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        error_str = str(error)
        self.state.end_tool(f"Error: {error_str}")
        try:
            ctx = RequestContext.current()
            if ctx:
                ctx.record_tool()
        except Exception:
            pass
        self.logger.error("T", "工具错误", error=error_str, exc_info=True)

    def on_agent_action(self, action, **kwargs):
        if self.is_debug:
            self.logger.debug("A", "Agent 动作", action=str(action)[:200])

    def on_agent_finish(self, finish, **kwargs):
        if self.is_debug:
            output = str(finish.return_values.get('output', '')) if hasattr(finish, 'return_values') else str(finish)
            self.logger.debug("A", "Agent 完成", output=output[:200])


async def generate_partial_summary(
    state: ExecutionState,
    step_purpose: str,
    instruction: str,
    llm,
    logger
) -> str:
    from agents.common import generate_partial_summary as common_summary
    user_input = f"{step_purpose}\n指令: {instruction}"
    return await common_summary(user_input, state, llm, logger, reason=step_purpose)

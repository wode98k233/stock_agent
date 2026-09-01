"""
选股雷达 - 执行器回调模块
用于捕获 React Agent 的中间状态，即使达到递归限制也能提取已有结果
同时负责 tool 指标采集和调试日志（合并原 MetricsCallback + AgentDebugCallback）
"""
import json
from typing import List, Dict, Any, Optional
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from config import Config
from utils.logger import RequestContext, ensure_radar


def is_tool_success(tool_output: Optional[str]) -> bool:
    """判断工具输出是否代表成功。

    部分 skill 会把异常包装成普通文本返回，例如“工具 xxx 失败...请勿重试”。
    这类结果不能记为成功，否则 ReAct 会误以为数据已正常获取。
    """
    if tool_output is None:
        return False
    text = str(tool_output).strip()
    if not text:
        return False
    lower = text.lower()
    failure_prefixes = ("执行失败", "执行异常", "error:", "exception:")
    if lower.startswith(failure_prefixes) or text.startswith(("执行失败", "执行异常")):
        return False
    if text.startswith("工具 ") and "失败" in text[:120]:
        return False
    if "使用方式错误" in text or "请勿重试" in text or "不要重试" in text:
        return False
    if "已跳过" in text or "不支持" in text:
        return False
    if "traceback" in lower:
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        return True

    def _is_empty_or_error_like(value) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return True
            lowered = stripped.lower()
            return (
                lowered.startswith(("error:", "exception:"))
                or "失败" in stripped
                or "未获取到" in stripped
                or "无法获取" in stripped
                or "不支持" in stripped
                or "已跳过" in stripped
            )
        if isinstance(value, list):
            return len(value) == 0 or all(_is_empty_or_error_like(item) for item in value)
        if isinstance(value, dict):
            if str(value.get("status", "")).lower() == "failed":
                return True
            if "error" in value:
                return True
            useful_keys = set(value.keys()) - {"code", "symbol", "query", "status"}
            if not useful_keys:
                return True
            return all(_is_empty_or_error_like(value.get(key)) for key in useful_keys)
        return False

    if isinstance(parsed, dict):
        if str(parsed.get("status", "")).lower() == "failed":
            return False
        if "error" in parsed:
            return False
        useful_keys = set(parsed.keys()) - {"code", "symbol", "query", "status"}
        if not useful_keys:
            return False
        if all(_is_empty_or_error_like(parsed.get(key)) for key in useful_keys):
            return False
    elif isinstance(parsed, list) and len(parsed) == 0:
        return False
    return True


class ToolCall:
    """工具调用记录"""
    def __init__(self, tool_name: str, tool_input: str, tool_output: Optional[str] = None,
                 tool_call_id: Optional[str] = None, raw_output: Optional[str] = None):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.tool_output = tool_output
        self.tool_call_id = tool_call_id
        self.raw_output = raw_output
        self.success = is_tool_success(tool_output)


class ExecutionState:
    """执行状态捕获器"""
    def __init__(self):
        self.messages: List[BaseMessage] = []
        self.tool_calls: List[ToolCall] = []
        self.current_tool_name: Optional[str] = None
        self.current_tool_input: Optional[str] = None
        self.pending_tools: Dict[str, ToolCall] = {}
        self.completed_tool_ids: set[str] = set()
        self.iteration_count = 0
        self.max_iterations_reached = False
        self.react_context_summaries: List[dict] = []
        self.react_context_summarized_until = 0
        self.react_context_summary_updates = 0

    def reset(self):
        self.messages = []
        self.tool_calls = []
        self.current_tool_name = None
        self.current_tool_input = None
        self.pending_tools = {}
        self.completed_tool_ids = set()
        self.iteration_count = 0
        self.max_iterations_reached = False
        self.react_context_summaries = []
        self.react_context_summarized_until = 0
        self.react_context_summary_updates = 0

    def add_message(self, message: BaseMessage):
        self.messages.append(message)

    def start_tool(self, tool_name: str, tool_input: str, tool_call_id: Optional[str] = None):
        if tool_call_id:
            if tool_call_id in self.completed_tool_ids:
                return
            self.pending_tools[tool_call_id] = ToolCall(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_call_id=tool_call_id,
            )
            return
        self.current_tool_name = tool_name
        self.current_tool_input = tool_input

    def end_tool(self, tool_output: str, tool_call_id: Optional[str] = None, raw_output: Optional[str] = None):
        if tool_call_id and tool_call_id in self.completed_tool_ids and tool_call_id not in self.pending_tools:
            return
        if tool_call_id and tool_call_id in self.pending_tools:
            tool_call = self.pending_tools.pop(tool_call_id)
            tool_call.tool_output = tool_output
            tool_call.raw_output = raw_output
            tool_call.success = is_tool_success(tool_output)
            self.tool_calls.append(tool_call)
            self.completed_tool_ids.add(tool_call_id)
            return
        if self.current_tool_name and self.current_tool_input:
            tool_call = ToolCall(
                tool_name=self.current_tool_name,
                tool_input=self.current_tool_input,
                tool_output=tool_output,
                raw_output=raw_output,
            )
            self.tool_calls.append(tool_call)
            self.current_tool_name = None
            self.current_tool_input = None

    def find_completed_tool(self, tool_name: str, tool_input: str, tool_output: Optional[str] = None) -> Optional[ToolCall]:
        """查找 callback 已记录的同一次工具执行。"""
        for call in reversed(self.tool_calls):
            if call.tool_name != tool_name or call.tool_input != tool_input:
                continue
            if tool_output is not None and str(call.tool_output or "") != str(tool_output):
                continue
            return call
        return None

    def update_tool_call(self, call: ToolCall, tool_output: str, raw_output: Optional[str] = None):
        """更新已记录工具输出，用于 tool node 压缩后覆盖 callback 原始输出。"""
        call.tool_output = tool_output
        call.raw_output = raw_output
        call.success = is_tool_success(tool_output)

    def update_tool_call_by_id(self, tool_call_id: str, tool_output: str, raw_output: Optional[str] = None) -> bool:
        """通过 tool_call_id 精确更新工具输出（并发安全）。"""
        for tc in reversed(self.tool_calls):
            if tc.tool_call_id == tool_call_id:
                tc.tool_output = tool_output
                tc.raw_output = raw_output
                tc.success = is_tool_success(tool_output)
                return True
        return False

    def increment_iteration(self):
        self.iteration_count += 1

    def rebuild_from_messages(self, messages: list):
        """从 checkpoint 恢复的 messages 重建 tool_calls 和压缩进度。

        checkpoint 恢复后 ExecutionState 是全新空实例，但 state["messages"]
        包含完整历史。此方法解析 messages 重建关键字段，避免：
        - 压缩器重复压缩已处理的轮次
        - 工具去重失效
        - cache_hit 检测失效
        """
        from langchain_core.messages import AIMessage, ToolMessage

        tool_calls_by_id: dict[str, ToolCall] = {}
        tool_outputs: dict[str, str] = {}

        for msg in messages:
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    tc_id = tc.get("id", "")
                    if tc_id and tc_id not in tool_calls_by_id:
                        tool_calls_by_id[tc_id] = ToolCall(
                            tool_name=tc.get("name", ""),
                            tool_input=str(tc.get("args", "")),
                            tool_call_id=tc_id,
                        )
            elif isinstance(msg, ToolMessage):
                tc_id = getattr(msg, "tool_call_id", "")
                if tc_id:
                    tool_outputs[tc_id] = msg.content

        # 匹配 tool_call 和 tool_output
        self.tool_calls = []
        self.completed_tool_ids = set()
        for tc_id, tc in tool_calls_by_id.items():
            if tc_id in tool_outputs:
                tc.tool_output = tool_outputs[tc_id]
                tc.success = is_tool_success(tool_outputs[tc_id])
                self.tool_calls.append(tc)
                self.completed_tool_ids.add(tc_id)

        # 压缩进度：标记所有已有消息为"已处理"，避免重复压缩
        self.react_context_summarized_until = len(messages)
        self.react_context_summaries = []  # 恢复后清空历史摘要列表，下次压缩时从零开始重新生成

    def get_summary_context(self) -> str:
        parts = []

        if self.tool_calls:
            # 去重：相同 tool_name+tool_input 只保留最后一次（结果最新/最完整）
            seen = {}
            deduped = []
            for call in self.tool_calls:
                sig = f"{call.tool_name}:{call.tool_input}"
                seen[sig] = call
            # 按首次出现顺序输出去重后的结果
            seen_order = set()
            for call in self.tool_calls:
                sig = f"{call.tool_name}:{call.tool_input}"
                if sig not in seen_order:
                    seen_order.add(sig)
                    deduped.append(seen[sig])

            parts.append("已执行的工具调用：")
            for i, call in enumerate(deduped, 1):
                status = "✅" if call.success else "❌"
                parts.append(f"\n{status} 工具 {i}: {call.tool_name}")
                parts.append(f"   输入: {call.tool_input}...")
                if call.tool_output:
                    parts.append(f"   输出: {call.tool_output}...")

        last_ai_msg = None
        for msg in reversed(self.messages):
            if isinstance(msg, AIMessage):
                last_ai_msg = msg
                break

        if last_ai_msg:
            parts.append(f"\n\n最后的分析结果：\n{last_ai_msg.content}...")

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
        if self.is_debug:
            self.logger.debug("X", f"LLM 调用 #{self.state.iteration_count}")

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        if self.is_debug:
            self.logger.debug("X", "LLM 调用结束")

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        tool_name = serialized.get('name', 'unknown')
        self.state.start_tool(tool_name, input_str, tool_call_id=str(run_id))
        if self.is_debug:
            self.logger.debug("T", f"→ 工具开始: {tool_name}", input=str(input_str))

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        output_str = str(output.content) if hasattr(output, 'content') else str(output)
        self.state.end_tool(output_str, tool_call_id=str(run_id))
        try:
            ctx = RequestContext.current()
            if ctx:
                ctx.record_tool()
        except Exception:
            pass
        if self.is_debug:
            self.logger.debug("T", "← 工具完成", output_len=len(output_str), output=output_str)

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        error_str = str(error)
        self.state.end_tool(f"Error: {error_str}", tool_call_id=str(run_id))
        try:
            ctx = RequestContext.current()
            if ctx:
                ctx.record_tool()
        except Exception:
            pass
        self.logger.error("T", "工具错误", error=error_str, exc_info=True)

    def on_agent_action(self, action, **kwargs):
        if self.is_debug:
            self.logger.debug("A", "Agent 动作", action=str(action))

    def on_agent_finish(self, finish, **kwargs):
        if self.is_debug:
            output = str(finish.return_values.get('output', '')) if hasattr(finish, 'return_values') else str(finish)
            self.logger.debug("A", "Agent 完成", output=output)


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

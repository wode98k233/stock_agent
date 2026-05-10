"""
选股雷达 - 进度事件系统
支持流式推送分析过程中的中间状态
"""
import time
from dataclasses import dataclass, asdict
from typing import Optional, Any, Callable
from enum import Enum


class ProgressType(str, Enum):
    """进度事件类型"""
    START = "start"
    STEP_START = "step_start"
    STEP_COMPLETE = "step_complete"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MILESTONE = "milestone"
    WARNING = "warning"
    ERROR = "error"
    FINAL = "final"
    CLASSIFIER = "classifier"
    PLANNER = "planner"
    REPLANNER = "replanner"
    LLM_CALL = "llm_call"


# 统一的图标映射常量
PROGRESS_TYPE_ICONS = {
    ProgressType.START: "🔍",
    ProgressType.STEP_START: "📋",
    ProgressType.STEP_COMPLETE: "✅",
    ProgressType.TOOL_CALL: "🔧",
    ProgressType.TOOL_RESULT: "📦",
    ProgressType.MILESTONE: "🎯",
    ProgressType.WARNING: "⚠️",
    ProgressType.ERROR: "❌",
    ProgressType.FINAL: "📊",
    ProgressType.CLASSIFIER: "🏷️",
    ProgressType.PLANNER: "📝",
    ProgressType.REPLANNER: "🔄",
    ProgressType.LLM_CALL: "💬",
}


@dataclass
class ProgressEvent:
    """进度事件"""
    type: ProgressType
    message: str
    step: int = 0
    total_steps: int = 0
    data: Optional[Any] = None
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['type'] = self.type.value
        return d


class ProgressReporter:
    """进度报告器"""

    def __init__(self, callback: Optional[Callable] = None):
        """
        :param callback: 事件回调函数，签名为 callback(event: ProgressEvent)
        """
        self.callback = callback
        self.events = []

    def report(self, event_type: ProgressType, message: str, **kwargs):
        """发送进度事件"""
        event = ProgressEvent(
            type=event_type,
            message=message,
            timestamp=time.time(),
            **kwargs
        )
        self.events.append(event)
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass

    def _print_event(self, event: ProgressEvent):
        """默认的打印回调"""
        icon = PROGRESS_TYPE_ICONS.get(event.type, "•")
        print(f"\r{icon} {event.message}", flush=True, end="")

    def start(self, total_steps: int = 0):
        self.report(ProgressType.START, f"🔍 开始分析...", total_steps=total_steps)

    def classifier_result(self, is_stock_related: bool, complexity: str = "unknown"):
        msg = f"🏷️ 分类: {'股票相关' if is_stock_related else '非股票相关'} | 复杂度: {complexity}"
        self.report(ProgressType.CLASSIFIER, msg)

    def planner_result(self, step_count: int):
        self.report(ProgressType.PLANNER, f"📝 计划生成: {step_count} 步")

    def replanner_result(self, action: str):
        self.report(ProgressType.REPLANNER, f"🔄 重规划: {action}")

    def step_start(self, step: int, description: str):
        self.report(ProgressType.STEP_START, f"📋 步骤 {step}: {description}", step=step)

    def step_complete(self, step: int, summary: str):
        self.report(ProgressType.STEP_COMPLETE, f"✅ 步骤 {step} 完成: {summary}", step=step)

    def tool_call(self, tool_name: str, query: str):
        query_preview = query[:50] + "..." if len(query) > 50 else query
        self.report(ProgressType.TOOL_CALL, f"🔧 调用 {tool_name}: {query_preview}")

    def tool_result(self, tool_name: str, summary: str):
        self.report(ProgressType.TOOL_RESULT, f"📦 {tool_name} 返回: {summary}")

    def milestone(self, message: str, data: Any = None):
        self.report(ProgressType.MILESTONE, f"🎯 {message}", data=data)

    def warning(self, message: str):
        self.report(ProgressType.WARNING, f"⚠️ {message}")

    def error(self, message: str):
        self.report(ProgressType.ERROR, f"❌ {message}")

    def llm_call(self, label: str, duration: float, tokens: str):
        self.report(ProgressType.LLM_CALL, f"💬 {label} | {duration:.1f}s | {tokens}")

    def final(self, message: str):
        self.report(ProgressType.FINAL, message)


def default_progress_callback(event: ProgressEvent):
    """默认的进度打印回调"""
    icon = PROGRESS_TYPE_ICONS.get(event.type, "•")
    print(f"\r{icon} {event.message}", flush=True, end="")

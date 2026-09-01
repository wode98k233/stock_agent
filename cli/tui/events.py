"""TUI 事件类型 — v2 版本：结构化步骤 + 指标 + 写操作确认。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from textual.message import Message


@dataclass
class TUIEvent(Message):
    """TUI 事件基类，继承 Message 以便直接 post_message。"""

    def __post_init__(self) -> None:
        Message.__post_init__(self)


@dataclass
class TimelineStepEvent(TUIEvent):
    """步骤事件：替换 ProgressEvent。"""
    step: str = ""
    status: str = ""
    elapsed: str = ""
    duration_ms: int = 0
    detail: str = ""


@dataclass
class MetricEvent(TUIEvent):
    """指标更新事件。"""
    name: str = ""
    value: Any = None


@dataclass
class TaskStartEvent(TUIEvent):
    """任务开始事件。"""
    task_id: str = ""
    mode: str = ""
    question: str = ""


@dataclass
class TaskEndEvent(TUIEvent):
    """任务结束事件。"""
    task_id: str = ""
    status: str = ""
    duration: float = 0.0
    metrics: dict = field(default_factory=dict)


@dataclass
class ReportChunkEvent(TUIEvent):
    """报告块事件。"""
    content: str = ""
    final: bool = False


@dataclass
class ConfirmRequestEvent(TUIEvent):
    """写操作确认请求。"""
    title: str = ""
    summary: str = ""
    diff: list = field(default_factory=list)
    backup_path: str = ""
    on_apply: Callable | None = None
    on_cancel: Callable | None = None


@dataclass
class ErrorEvent(TUIEvent):
    """错误事件。"""
    message: str = ""
    recoverable: bool = True


@dataclass
class UnlockInputEvent(TUIEvent):
    """解锁输入区。"""
    pass


# ── 向后兼容 shim（旧名 → 新名） ─────────────────────────────


@dataclass
class ProgressEvent(TUIEvent):
    """旧版 ProgressEvent，保留字段名以便渐进迁移。"""
    step: str = ""
    status: str = ""
    elapsed: str = ""
    description: str = ""
    task_id: str = ""


@dataclass
class ReportEvent(TUIEvent):
    """旧版 ReportEvent，保留为 ReportChunkEvent 的单块形态。"""
    content: str = ""


@dataclass
class SummaryEvent(TUIEvent):
    """旧版 SummaryEvent，保留字段。"""
    task_id: str = ""
    tokens: int = 0
    llm_calls: int = 0
    tools: int = 0
    time: str = ""
    log_path: str = ""


@dataclass
class ConfirmEvent(TUIEvent):
    """旧版 ConfirmEvent。"""
    prompt: str = ""
    callback: Any = None


@dataclass
class UnlockEvent(TUIEvent):
    """旧版 UnlockEvent。"""
    pass

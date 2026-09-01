"""
记忆系统 — 请求上下文透传
通过 contextvars 在 Web/CLI 层与 plugin 层之间传递会话信息，不改动 Agent 接口。
"""
import contextvars

# 当前请求的会话上下文
current_session_id: contextvars.ContextVar = contextvars.ContextVar(
    "memory_session_id", default=""
)
current_dialog_uuid: contextvars.ContextVar = contextvars.ContextVar(
    "memory_dialog_uuid", default=""
)
current_trace_run_id: contextvars.ContextVar = contextvars.ContextVar(
    "memory_trace_run_id", default=""
)

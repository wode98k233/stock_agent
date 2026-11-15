"""子代理（取数执行器）—— 数据结构。

设计：docs/arch/2026-10-31-long-report-agent-outline-design-m2.md §1
口径：
- 子代理**看不到父对话**（自足 goal），只回结构化结论卡；
- 失败落在 status/stop_reason 上，**不抛异常穿透**；
- diagnostic ≤4096 字节（唯一允许截断的地方）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

DIAG_MAX = 4096

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_ABORTED = "aborted"
STATUS_MAX_TURNS = "max_turns"

STOP_COMPLETED = "completed"
STOP_ABORTED = "aborted"
STOP_ERROR = "error"
STOP_MAX_TOKENS = "max-tokens"
STOP_MAX_TURNS = "max-turns"
STOP_TIMEOUT = "timeout"
STOP_NO_DATA = "no-data"


@dataclass(frozen=True)
class SubagentTask:
    label: str
    slot: str
    goal: str
    output_schema: Dict[str, Any]
    max_turns: int = 12
    deadline_s: float = 180.0
    tool_whitelist: Tuple[str, ...] = ()


@dataclass
class SubagentResult:
    status: str
    stop_reason: str
    text: str = ""
    structured: Optional[Dict[str, Any]] = None
    tool_calls: int = 0
    turns: int = 0
    tokens: Dict[str, int] = field(default_factory=dict)
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "stop_reason": self.stop_reason,
            "text": self.text, "structured": self.structured,
            "tool_calls": self.tool_calls, "turns": self.turns,
            "tokens": self.tokens, "diagnostic": self.diagnostic,
        }


def clip_diagnostic(text: str) -> str:
    """diagnostic 上限 4096 字节（按 UTF-8 计），超出截断并标注。"""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= DIAG_MAX:
        return text
    clipped = raw[:DIAG_MAX].decode("utf-8", "ignore")
    return clipped + "…（已截断，完整见 trace）"

"""CLI 命令基础设施 — CommandResult、CommandHandler、确认流程"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cli.context import CLIContext


@dataclass
class CommandResult:
    """统一命令返回类型。"""

    ok: bool = True
    message: str = ""
    data: dict | list | None = None
    exit: bool = False
    requires_confirm: bool = False
    confirm_prompt: str = ""
    pending_action: Any = None  # 确认后执行的回调


class CommandHandler:
    """命令处理器基类。

    子类实现 `execute(ctx, args)` 方法。
    需要确认的写操作返回 `CommandResult(requires_confirm=True, ...)`，
    repl 层负责收集用户确认后调用 `pending_action()`。
    """

    name: str = ""
    description: str = ""
    usage: str = ""

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        raise NotImplementedError

    def help_text(self) -> str:
        lines = [f"  /{self.name}  —  {self.description}"]
        if self.usage:
            lines.append(f"    用法: {self.usage}")
        return "\n".join(lines)

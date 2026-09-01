"""CLI 命令：/session — 会话管理"""
from __future__ import annotations

import time

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import status_line, separator, err


class SessionCommand(CommandHandler):
    name = "session"
    description = "会话管理"
    usage = "/session stats | /session clear | /session exit"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "stats"

        if action == "stats":
            return self._stats(ctx)
        if action == "clear":
            return self._clear(ctx)
        if action == "exit":
            return CommandResult(ok=True, exit=True, message="")

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _stats(self, ctx: CLIContext) -> CommandResult:
        stats = ctx.session_stats
        if not stats:
            return CommandResult(ok=True, message="无会话统计。")

        duration = time.time() - stats.start_time if hasattr(stats, 'start_time') else 0
        if duration < 60:
            dur_str = f"{duration:.0f}s"
        else:
            mins, secs = divmod(int(duration), 60)
            dur_str = f"{mins}m {secs}s"

        cached = getattr(stats, 'total_cached_tokens', 0)
        lines = [
            separator(),
            status_line("查询次数", str(getattr(stats, 'queries', 0))),
            status_line("Token 消耗", f"{getattr(stats, 'total_tokens', 0):,}"),
            status_line("缓存命中", f"{cached:,}" + (f" ({cached / max(getattr(stats, 'total_tokens', 1), 1) * 100:.0f}%)" if cached else "")),
            status_line("LLM 调用", f"{getattr(stats, 'total_llm_calls', 0)} 次"),
            status_line("工具调用", f"{getattr(stats, 'total_tool_calls', 0)} 次"),
            status_line("会话时长", dur_str),
            separator(),
        ]
        return CommandResult(ok=True, message="\n".join(lines))

    def _clear(self, ctx: CLIContext) -> CommandResult:
        if ctx.memory:
            try:
                ctx.memory.clear()
                return CommandResult(ok=True, message="[OK] 会话记忆已清除。")
            except Exception as e:
                return CommandResult(ok=False, message=err(f"清除失败: {e}"))
        return CommandResult(ok=True, message="[OK] 无记忆需要清除。")

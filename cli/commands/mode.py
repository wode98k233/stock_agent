"""CLI 命令：/mode — Agent 模式管理"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, err


class ModeCommand(CommandHandler):
    name = "mode"
    description = "查看/切换 Agent 模式"
    usage = "/mode list | /mode use <name>"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        from agents.factory import AgentFactory

        action = args[0] if args else "list"

        if action == "list":
            modes = AgentFactory.list()
            current = ctx.agent_mode or "react_stock"
            rows = []
            for m in modes:
                marker = " *" if m == current else ""
                rows.append([m + marker, "当前" if m == current else ""])
            table = format_table(["模式", "状态"], rows)
            return CommandResult(ok=True, message=f"可用 Agent 模式：\n{table}")

        if action == "use":
            if len(args) < 2:
                return CommandResult(ok=False, message=err("用法: /mode use <name>"))
            mode_name = args[1]
            available = AgentFactory.list()
            if mode_name not in available:
                return CommandResult(ok=False, message=err(f"未知模式: {mode_name}\n可用: {', '.join(available)}"))
            ctx.agent_mode = mode_name
            return CommandResult(ok=True, message=f"[OK] 已切换到模式: {mode_name}")

        return CommandResult(ok=False, message=err(f"未知操作: {action}\n用法: /mode list | /mode use <name>"))

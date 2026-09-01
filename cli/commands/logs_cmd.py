"""CLI 命令：/logs — 日志查看"""
from __future__ import annotations

import os
import datetime

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, err


class LogsCommand(CommandHandler):
    name = "logs"
    description = "查看日志"
    usage = "/logs recent [--limit N] | /logs show <uuid>"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "recent"
        flags = flags or {}
        limit = int(flags.get("limit", 10))

        if action == "recent":
            return self._recent(limit)
        if action == "show":
            if len(args) < 2:
                return CommandResult(ok=False, message=err("用法: /logs show <uuid>"))
            return self._show(args[1])

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _recent(self, limit: int) -> CommandResult:
        from cli.log_utils import scan_recent_logs

        logs = scan_recent_logs(limit)
        if not logs:
            return CommandResult(ok=True, message="暂无日志。")

        rows = []
        for entry in logs:
            query = entry["query"]
            if len(query) > 40:
                query = query[:39] + "…"
            rows.append([entry["uuid"], query, entry["size"], entry["time"]])

        table = format_table(["uuid", "问题", "大小", "时间"], rows)
        return CommandResult(ok=True, message=f"最近日志：\n{table}")

    def _show(self, uuid: str) -> CommandResult:
        from utils.app_paths import get_logs_dir

        logs_dir = get_logs_dir()
        # 支持前缀匹配
        matches = []
        for f in os.listdir(logs_dir):
            if f.endswith(".log") and f.startswith(uuid):
                matches.append(os.path.join(logs_dir, f))

        if not matches:
            return CommandResult(ok=False, message=err(f"未找到日志: {uuid}"))
        if len(matches) > 1:
            return CommandResult(ok=False, message=err(f"前缀匹配到多个日志，请更精确。"))

        path = matches[0]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return CommandResult(ok=True, message=content)
        except Exception as e:
            return CommandResult(ok=False, message=err(f"读取失败: {e}"))


"""CLI 命令：/notify — 通知管理"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, format_json, status_line, separator, err


class NotifyCommand(CommandHandler):
    name = "notify"
    description = "通知管理"
    usage = "/notify status | /notify test --channel <name>"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "status"
        flags = flags or {}

        if action == "status":
            return self._status()
        if action == "test":
            channel = flags.get("channel", "")
            return self._test(channel)

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _status(self) -> CommandResult:
        from utils.notification.factory import NotificationFactory
        from utils.notification.manager import NotificationManager

        try:
            channels = NotificationFactory.create_channels()
            manager = NotificationManager()
            manager.register_channels(channels)
            status = manager.status()
        except Exception as e:
            return CommandResult(ok=False, message=err(f"获取通知状态失败: {e}"))

        enabled = status.get("enabled", False)
        available = status.get("channels", [])

        lines = [
            separator(),
            status_line("通知", "已启用" if enabled else "未启用"),
            status_line("渠道数", str(len(available))),
            separator(),
        ]

        if available:
            lines.append("")
            lines.append("可用渠道：")
            for ch in available:
                lines.append(f"  - {ch}")
        else:
            lines.append("")
            lines.append("未配置任何通知渠道。请设置对应的环境变量。")

        return CommandResult(ok=True, message="\n".join(lines))

    def _test(self, channel: str) -> CommandResult:
        if not channel:
            return CommandResult(ok=False, message=err("用法: /notify test --channel <name>"))

        from utils.notification.factory import NotificationFactory
        from utils.notification.manager import NotificationManager
        from utils.notification.base import NotificationMessage

        try:
            channels = NotificationFactory.create_channels()
            manager = NotificationManager()
            manager.register_channels(channels)

            message = NotificationMessage(
                title="选股雷达测试通知",
                content="这是一条测试消息，来自 CLI /notify test 命令。",
                report_id="cli-test",
            )
            results = manager.send(message, channels=[channel])
        except Exception as e:
            return CommandResult(ok=False, message=err(f"发送失败: {e}"))

        if not results:
            return CommandResult(ok=False, message=err(f"渠道不存在或未配置: {channel}"))

        r = results[0]
        if r.success:
            return CommandResult(ok=True, message=f"[OK] 测试通知已发送到 {channel}。")
        else:
            return CommandResult(ok=False, message=err(f"发送失败: {r.error}"))

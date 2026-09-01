"""CLI 命令：/help — 分层帮助"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext


_HELP_SECTIONS = {
    "": (
        "Stock Radar CLI 命令：\n"
        "\n"
        "  直接输入问题开始分析，或使用以下命令：\n"
        "\n"
        "  /help [namespace]       查看帮助（支持按命名空间）\n"
        "  /status [--json]        查看运行状态\n"
        "  /mode list|use <name>   查看/切换 Agent 模式\n"
        "  /config list|get|set    配置管理\n"
        "  /skills list|show       Skill 管理\n"
        "  /templates list|show    报告模板管理\n"
        "  /watchlist list|quote   自选股管理\n"
        "  /calendar today|month   交易日历\n"
        "  /notify status|test     通知管理\n"
        "  /trace recent|show      查看执行链\n"
        "  /logs recent|show       查看日志\n"
        "  /session stats|clear    会话管理\n"
        "\n"
        "  输入 exit 或 quit 退出。\n"
    ),
    "help": "  /help [namespace]  —  查看指定命名空间的帮助\n",
    "status": (
        "  /status [--json]  —  查看运行状态\n"
        "    显示当前模式、模型、预算、Skill 数量、模板数量、最近日志。\n"
    ),
    "mode": (
        "  /mode list           —  列出所有可用 Agent 模式\n"
        "  /mode use <name>     —  切换到指定模式\n"
    ),
    "trace": (
        "  /trace recent [--limit N]  —  查看最近 N 条执行链\n"
        "  /trace show <run_id>       —  查看指定执行链详情\n"
    ),
    "logs": (
        "  /logs recent [--limit N]  —  查看最近 N 条日志\n"
        "  /logs show <uuid>         —  查看指定请求日志\n"
    ),
    "session": (
        "  /session stats   —  查看当前会话统计\n"
        "  /session clear   —  清除会话记忆\n"
        "  /session exit    —  退出\n"
    ),
}


class HelpCommand(CommandHandler):
    name = "help"
    description = "查看帮助"
    usage = "/help [namespace]"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        ns = args[0] if args else ""
        text = _HELP_SECTIONS.get(ns)
        if text is None:
            available = ", ".join(sorted(_HELP_SECTIONS.keys()) if ns else
                                  [k for k in sorted(_HELP_SECTIONS.keys()) if k])
            text = f"未知命令空间: {ns}\n可用: {available}"
        return CommandResult(ok=True, message=text)

"""CLI 命令注册表"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cli.commands.base import CommandHandler

# 命名空间 → 处理器类（延迟导入避免循环）
_REGISTRY: dict[str, type[CommandHandler]] = {}


def register_command(namespace: str, handler_cls: type[CommandHandler]):
    _REGISTRY[namespace] = handler_cls


def get_handler(namespace: str) -> CommandHandler | None:
    cls = _REGISTRY.get(namespace)
    return cls() if cls else None


def all_handlers() -> dict[str, CommandHandler]:
    return {ns: cls() for ns, cls in _REGISTRY.items()}


def _ensure_registered():
    """延迟注册所有内置命令。"""
    if _REGISTRY:
        return
    from cli.commands.help import HelpCommand
    from cli.commands.status import StatusCommand
    from cli.commands.mode import ModeCommand
    from cli.commands.trace_cmd import TraceCommand
    from cli.commands.logs_cmd import LogsCommand
    from cli.commands.session import SessionCommand
    from cli.commands.config import ConfigCommand
    from cli.commands.skills import SkillsCommand
    from cli.commands.templates import TemplatesCommand
    from cli.commands.notify import NotifyCommand
    from cli.commands.watchlist import WatchlistCommand
    from cli.commands.calendar_cmd import CalendarCommand

    register_command("help", HelpCommand)
    register_command("status", StatusCommand)
    register_command("mode", ModeCommand)
    register_command("trace", TraceCommand)
    register_command("logs", LogsCommand)
    register_command("session", SessionCommand)
    register_command("config", ConfigCommand)
    register_command("skills", SkillsCommand)
    register_command("templates", TemplatesCommand)
    register_command("notify", NotifyCommand)
    register_command("watchlist", WatchlistCommand)
    register_command("calendar", CalendarCommand)


async def dispatch(namespace: str, action: str, args: list[str], flags: dict, ctx):
    """分发命令到对应处理器。action 拼到 args 前面。"""
    _ensure_registered()
    handler = get_handler(namespace)
    if handler:
        full_args = ([action] if action else []) + list(args)
        return await handler.execute(ctx, full_args, flags)
    return None

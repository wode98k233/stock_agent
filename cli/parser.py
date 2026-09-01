"""CLI 输入解析器 — 区分 / 命令、旧命令和自然语言"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field


@dataclass
class ParsedInput:
    """解析后的用户输入。"""

    kind: str  # "command" | "legacy" | "natural"
    namespace: str = ""  # / 后的第一段，如 "status"
    action: str = ""  # 子命令，如 "list"
    args: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)  # --key value
    raw: str = ""


# 旧命令兼容映射 → 新命名空间
_LEGACY_MAP = {
    "help": ("help", ""),
    "?": ("help", ""),
    "h": ("help", ""),
    "exit": ("session", "exit"),
    "exit()": ("session", "exit"),
    "quit": ("session", "exit"),
    "debug": ("status", "debug"),
    "info": ("status", ""),
    "mode": ("mode", ""),
    "model": ("config", "model"),
}

# 第一阶段支持的 / 命令
_KNOWN_NAMESPACES = {
    "help", "status", "mode", "trace", "logs",
    "config", "skills", "templates", "watchlist",
    "calendar", "notify", "session",
}


def parse_input(raw: str) -> ParsedInput:
    """解析用户输入，返回结构化结果。"""
    text = raw.strip()
    if not text:
        return ParsedInput(kind="natural", raw=raw)

    # / 命令
    if text.startswith("/"):
        return _parse_slash_command(text, raw)

    # 旧命令兼容（精确匹配）
    lower = text.lower()
    if lower in _LEGACY_MAP:
        ns, action = _LEGACY_MAP[lower]
        return ParsedInput(
            kind="legacy", namespace=ns, action=action, raw=raw,
        )

    # mode xxx 快捷方式
    if lower.startswith("mode "):
        mode_name = text[5:].strip()
        return ParsedInput(
            kind="legacy", namespace="mode", action="use",
            args=[mode_name], raw=raw,
        )

    # 自然语言
    return ParsedInput(kind="natural", raw=raw)


def _parse_slash_command(text: str, raw: str) -> ParsedInput:
    """解析 /namespace action [args] [--flags] 格式。"""
    # 去掉前导 /
    body = text[1:].strip()
    if not body:
        return ParsedInput(kind="command", namespace="help", raw=raw)

    try:
        tokens = shlex.split(body)
    except ValueError:
        tokens = body.split()

    if not tokens:
        return ParsedInput(kind="command", namespace="help", raw=raw)

    namespace = tokens[0].lower()
    action = ""
    args = []
    flags = {}

    remaining = tokens[1:]
    i = 0
    while i < len(remaining):
        token = remaining[i]
        if token.startswith("--"):
            key = token[2:]
            if i + 1 < len(remaining) and not remaining[i + 1].startswith("--"):
                flags[key] = remaining[i + 1]
                i += 2
            else:
                flags[key] = "true"
                i += 1
        elif not action:
            action = token.lower()
            i += 1
        else:
            args.append(token)
            i += 1

    return ParsedInput(
        kind="command",
        namespace=namespace,
        action=action,
        args=args,
        flags=flags,
        raw=raw,
    )

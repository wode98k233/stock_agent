"""CLI 命令：/config — 配置查看与修改"""
from __future__ import annotations

import os

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, format_json, status_line, separator, err, warn


class ConfigCommand(CommandHandler):
    name = "config"
    description = "配置管理"
    usage = "/config list [prefix] | /config get KEY | /config set KEY VALUE | /config diff"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "list"

        if action == "list":
            prefix = args[1] if len(args) > 1 else ""
            return self._list(prefix)
        if action == "get":
            if len(args) < 2:
                return CommandResult(ok=False, message=err("用法: /config get KEY"))
            return self._get(args[1])
        if action == "set":
            if len(args) < 3:
                return CommandResult(ok=False, message=err("用法: /config set KEY VALUE"))
            return self._set(ctx, args[1], " ".join(args[2:]))
        if action == "diff":
            return self._diff()

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _list(self, prefix: str) -> CommandResult:
        from server.config_schema import get_config_schema, mask_sensitive

        schema = get_config_schema()
        rows = []
        for item in schema:
            key = item.get("key", "")
            if prefix and not key.lower().startswith(prefix.lower()):
                continue
            label = item.get("label", "")
            value = os.getenv(key, "")
            if item.get("sensitive"):
                value = mask_sensitive(key, value)
            group = item.get("group", "")
            rows.append([key, value[:30], group])

        if not rows:
            return CommandResult(ok=True, message=f"无匹配配置" + (f"（前缀: {prefix}）" if prefix else ""))

        table = format_table(["KEY", "VALUE", "GROUP"], rows)
        return CommandResult(ok=True, message=f"配置列表：\n{table}")

    def _get(self, key: str) -> CommandResult:
        from server.config_schema import mask_sensitive
        from utils.env_helper import get_env

        value = get_env(key)
        if not value:
            return CommandResult(ok=False, message=err(f"配置项不存在: {key}"))

        # 检查是否敏感
        from server.config_schema import get_config_schema
        schema = get_config_schema()
        is_sensitive = any(s.get("key") == key and s.get("sensitive") for s in schema)
        display_value = mask_sensitive(key, value) if is_sensitive else value

        return CommandResult(ok=True, message=f"{key} = {display_value}")

    def _set(self, ctx: CLIContext, key: str, value: str) -> CommandResult:
        """设置配置项（需要确认）。"""
        current = os.getenv(key, "")
        if current == value:
            return CommandResult(ok=True, message=f"{key} 已是该值，无需修改。")

        def do_set():
            _write_env_key(key, value)
            os.environ[key] = value
            return CommandResult(ok=True, message=f"[OK] {key} = {value}")

        return CommandResult(
            ok=True,
            requires_confirm=True,
            confirm_prompt=f"将修改配置：\n  {key}: {current!r} -> {value!r}\n\n输入 apply 确认，输入 cancel 放弃：",
            pending_action=do_set,
        )

    def _diff(self) -> CommandResult:
        from utils.env_helper import get_env_diff

        diff = get_env_diff()
        if not diff:
            return CommandResult(ok=True, message="配置文件与当前环境变量无差异。")

        rows = []
        for key, (file_val, proc_val) in diff.items():
            rows.append([key, file_val[:25], proc_val[:25]])

        table = format_table(["KEY", "文件值", "进程值"], rows)
        return CommandResult(ok=True, message=f"配置差异：\n{table}")


def _write_env_key(key: str, value: str):
    """写入单个配置项到 .env 文件。"""
    from utils.env_helper import get_env_path

    env_path = get_env_path()
    lines = []
    found = False

    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)

    if not found:
        lines.append(f"{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

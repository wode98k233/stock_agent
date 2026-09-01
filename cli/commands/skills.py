"""CLI 命令：/skills — Skill 管理"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, format_json, separator, err


class SkillsCommand(CommandHandler):
    name = "skills"
    description = "Skill 管理"
    usage = "/skills list | /skills show <name> | /skills rescan"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "list"

        if action == "list":
            return self._list(ctx)
        if action == "show":
            if len(args) < 2:
                return CommandResult(ok=False, message=err("用法: /skills show <name>"))
            return self._show(ctx, args[1])
        if action == "rescan":
            return self._rescan(ctx)

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _list(self, ctx: CLIContext) -> CommandResult:
        reg = ctx.skill_register
        if not reg:
            return CommandResult(ok=False, message=err("SkillRegister 未初始化"))

        skills = reg.get_all_skills_unchecked()
        if not skills:
            return CommandResult(ok=True, message="未发现任何 Skill。")

        rows = []
        for name, meta in sorted(skills.items()):
            source = getattr(meta, 'source', 'internal')
            enabled = "Y" if getattr(meta, 'enabled', True) else "N"
            tool_count = len(getattr(meta, 'tools', []) or [])
            desc = getattr(meta, 'skill_desc', '') or getattr(meta, 'description', '') or ''
            rows.append([name, source, enabled, str(tool_count), desc])

        table = format_table(["名称", "来源", "状态", "工具数", "描述"], rows)
        return CommandResult(ok=True, message=f"已发现 {len(skills)} 个 Skill：\n{table}")

    def _show(self, ctx: CLIContext, name: str) -> CommandResult:
        reg = ctx.skill_register
        if not reg:
            return CommandResult(ok=False, message=err("SkillRegister 未初始化"))

        detail = reg.get_skill_detail(name)
        if not detail:
            return CommandResult(ok=False, message=err(f"未找到 Skill: {name}"))

        return CommandResult(ok=True, message=format_json(detail))

    def _rescan(self, ctx: CLIContext) -> CommandResult:
        reg = ctx.skill_register
        if not reg:
            return CommandResult(ok=False, message=err("SkillRegister 未初始化"))

        try:
            reg.rescan()
            count = len(reg.get_all_skills())
            return CommandResult(ok=True, message=f"[OK] 重新扫描完成，发现 {count} 个 Skill。")
        except Exception as e:
            return CommandResult(ok=False, message=err(f"扫描失败: {e}"))

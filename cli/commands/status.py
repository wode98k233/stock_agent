"""CLI 命令：/status — 运行状态"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import status_line, separator, format_json


class StatusCommand(CommandHandler):
    name = "status"
    description = "查看运行状态"
    usage = "/status [--json]"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        from config import Config
        from agents.factory import AgentFactory

        # 模式和模型
        mode = ctx.agent_mode or "react_stock"
        model = Config.OPENAI_MODEL_NAME
        trace_enabled = Config.ENABLE_TRACE

        # Skill 和模板数量
        skill_count = 0
        if ctx.skill_register:
            try:
                skills = ctx.skill_register.get_all_skills()
                skill_count = len(skills)
            except Exception:
                pass

        template_count = 0
        try:
            from agents.analysis.template_store import _load_index
            templates = _load_index()
            template_count = len(templates) if templates else 0
        except Exception:
            pass

        # 缓存状态
        cache_enabled = Config.CACHE_PREFIX_ENABLED

        # 最近日志
        recent_logs = _get_recent_logs(3)

        if "json" in args or "--json" in str(args):
            data = {
                "mode": mode,
                "model": model,
                "trace": "on" if trace_enabled else "off",
                "skills": skill_count,
                "templates": template_count,
                "cache": "enabled" if cache_enabled else "disabled",
                "recent_logs": recent_logs,
            }
            return CommandResult(ok=True, message=format_json(data), data=data)

        lines = [
            separator(),
            status_line("mode", mode),
            status_line("model", model),
            status_line("trace", "on" if trace_enabled else "off"),
            status_line("skills", f"{skill_count} loaded"),
            status_line("templates", f"{template_count} active"),
            status_line("cache", "enabled" if cache_enabled else "disabled"),
            separator(),
        ]

        if recent_logs:
            lines.append("")
            lines.append("最近日志：")
            for log in recent_logs:
                lines.append(f"  {log['uuid']}  {log['query']}  ({log['time']})")

        return CommandResult(ok=True, message="\n".join(lines))


def _get_recent_logs(limit: int = 3) -> list[dict]:
    """获取最近的请求日志文件。"""
    from cli.log_utils import scan_recent_logs
    return scan_recent_logs(limit)

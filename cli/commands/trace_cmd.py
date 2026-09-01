"""CLI 命令：/trace — 执行链查看"""
from __future__ import annotations

import json

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, err

_STATUS_ICON = {"success": "✅", "error": "❌", "running": "⏳"}
_STEP_STATUS_ICON = {"success": "✅", "error": "❌"}


class TraceCommand(CommandHandler):
    name = "trace"
    description = "查看执行链"
    usage = "/trace recent [--limit N] | /trace show <run_id> [--verbose]"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "recent"
        flags = flags or {}
        limit = int(flags.get("limit", 10))
        verbose = flags.get("verbose") or flags.get("v") or False

        if action == "recent":
            return self._recent(limit)
        if action == "show":
            show_args = [a for a in args[1:] if not a.startswith("-")]
            if not show_args:
                return CommandResult(ok=False, message=err("用法: /trace show <run_id> [--verbose]"))
            return self._show(show_args[0], verbose=bool(verbose))

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _recent(self, limit: int) -> CommandResult:
        from utils.agent_trace.db import get_trace_conn

        conn = get_trace_conn()
        if conn is None:
            return CommandResult(ok=True, message="Trace 数据库不存在或未初始化。")

        try:
            rows = conn.execute(
                "SELECT id, agent_name, status, duration_ms, created_at "
                "FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception as e:
            return CommandResult(ok=False, message=err(f"查询失败: {e}"))

        if not rows:
            return CommandResult(ok=True, message="暂无执行记录。")

        table_rows = []
        for r in rows:
            run_id = str(r["id"]) if r["id"] else ""
            agent = r["agent_name"] or ""
            status_icon = _STATUS_ICON.get(r["status"], "?")
            status = f"{status_icon} {r['status']}"
            duration = f"{r['duration_ms'] / 1000:.1f}s" if r["duration_ms"] else "—"
            created = (r["created_at"] or "")[:16]
            table_rows.append([run_id, agent, status, duration, created])

        table = format_table(["run_id", "Agent", "状态", "耗时", "创建时间"], table_rows)
        return CommandResult(ok=True, message=f"最近执行链：\n{table}")

    def _show(self, run_id: str, verbose: bool = False) -> CommandResult:
        from utils.agent_trace.db import get_trace_conn
        from utils.agent_trace.models import _TYPE_ICONS

        conn = get_trace_conn()
        if conn is None:
            return CommandResult(ok=False, message=err("Trace 数据库不存在。"))

        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
                (f"{run_id}%",),
            ).fetchone()
        except Exception as e:
            return CommandResult(ok=False, message=err(f"查询失败: {e}"))

        if not row:
            return CommandResult(ok=False, message=err(f"未找到 run_id: {run_id}"))

        run_dict = dict(row)
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id",
            (run_dict["id"],),
        ).fetchall()
        steps_dicts = [dict(s) for s in steps]

        tree_text = _build_tree(run_dict, steps_dicts, verbose)
        return CommandResult(ok=True, message=tree_text)


def _is_noise(step: dict) -> bool:
    extra = step.get("extra")
    if not extra:
        return False
    try:
        d = json.loads(extra)
        return d.get("_noise", False)
    except (json.JSONDecodeError, TypeError):
        return False


def _build_tree(run_dict: dict, steps_dicts: list[dict], verbose: bool = False) -> str:
    from utils.agent_trace.models import _TYPE_ICONS

    children: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for s in steps_dicts:
        pid = s["parent_run_id"]
        if pid:
            children.setdefault(pid, []).append(s)
        else:
            roots.append(s)

    icon = _STATUS_ICON.get(run_dict["status"], "?")
    ms = f"{run_dict['duration_ms'] / 1000:.1f}s" if run_dict["duration_ms"] else "—"
    lines = [
        f"🌳 Run: {run_dict['id'][:8]}  Agent: {run_dict['agent_name']}  {icon} {ms}",
        "─" * 50,
    ]

    def _print_tree(nodes: list[dict], prefix: str = "") -> None:
        for i, s in enumerate(nodes):
            is_last = i == len(nodes) - 1
            is_noise = _is_noise(s)

            if is_noise and not verbose:
                kids = children.get(s["event_run_id"], [])
                if kids:
                    _print_tree(kids, prefix)
                continue

            connector = "└─ " if is_last else "├─ "
            s_icon = _STEP_STATUS_ICON.get(s["status"], "⏳")
            s_ms = f"{s['duration_ms']:.0f}ms" if s["duration_ms"] else "—"
            type_icon = _TYPE_ICONS.get(s["step_type"], "?")
            name = s.get("step_name", "?")[:30]
            noise_marker = "🔇 " if is_noise else ""
            lines.append(f"{prefix}{connector}{type_icon} {noise_marker}{name}  {s_icon} {s_ms}")

            ext = "   " if is_last else "│  "
            kids = children.get(s["event_run_id"], [])
            if kids:
                _print_tree(kids, prefix + ext)

    _print_tree(roots)
    return "\n".join(lines)

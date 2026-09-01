"""CLI 命令：/templates — 报告模板管理"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, format_json, separator, err


class TemplatesCommand(CommandHandler):
    name = "templates"
    description = "报告模板管理"
    usage = "/templates list | /templates show <id> | /templates validate"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "list"

        if action == "list":
            return self._list()
        if action == "show":
            if len(args) < 2:
                return CommandResult(ok=False, message=err("用法: /templates show <id>"))
            return self._show(args[1])
        if action == "validate":
            return self._validate()

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _list(self) -> CommandResult:
        from agents.analysis.template_store import _load_index

        index = _load_index()
        templates = index.get("templates", {}) if isinstance(index, dict) else index
        if not templates:
            return CommandResult(ok=True, message="未发现任何报告模板。")

        rows = []
        for tid, meta in sorted(templates.items()):
            if isinstance(meta, str):
                continue
            name = meta.get("name", tid)
            desc = (meta.get("description", "") or "")[:35]
            rows.append([tid, name, desc])

        table = format_table(["ID", "名称", "描述"], rows)
        return CommandResult(ok=True, message=f"已发现 {len(templates)} 个模板：\n{table}")

    def _show(self, template_id: str) -> CommandResult:
        from agents.analysis.template_store import load_template

        try:
            template = load_template(template_id)
        except Exception as e:
            return CommandResult(ok=False, message=err(f"加载模板失败: {e}"))

        if not template:
            return CommandResult(ok=False, message=err(f"未找到模板: {template_id}"))

        # 简化输出
        info = {
            "id": template.get("id"),
            "name": template.get("name"),
            "version": template.get("version"),
            "role": (template.get("role") or "")[:80],
            "sections": [
                {"id": s.get("id"), "title": s.get("title"), "required": s.get("required")}
                for s in template.get("sections", [])
            ],
            "data_contract": [
                {"slot": d.get("slot"), "hard_required": d.get("hard_required")}
                for d in template.get("data_contract", [])
            ],
            "qa_rules": template.get("qa_rules", []),
        }
        return CommandResult(ok=True, message=format_json(info))

    def _validate(self) -> CommandResult:
        from agents.analysis.template_store import _load_index, load_template

        index = _load_index()
        templates = index.get("templates", {}) if isinstance(index, dict) else index
        if not templates:
            return CommandResult(ok=True, message="无模板可验证。")

        results = []
        for tid in templates:
            try:
                t = load_template(tid)
                sections = t.get("sections", [])
                required = [s for s in sections if s.get("required")]
                results.append([tid, "OK", f"{len(sections)} sections, {len(required)} required"])
            except Exception as e:
                results.append([tid, "ERR", str(e)[:40]])

        table = format_table(["ID", "状态", "详情"], results)
        return CommandResult(ok=True, message=f"模板验证结果：\n{table}")

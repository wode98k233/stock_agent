"""CLI 命令：/calendar — 交易日历"""
from __future__ import annotations

from datetime import datetime

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, err


class CalendarCommand(CommandHandler):
    name = "calendar"
    description = "交易日历"
    usage = "/calendar today | /calendar month [YYYY-MM]"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "today"

        if action == "today":
            return self._today()
        if action == "month":
            period = args[1] if len(args) > 1 else ""
            return self._month(period)

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _today(self) -> CommandResult:
        from utils.trading_calendar import is_market_open, get_market_status_text

        today = datetime.now().strftime("%Y-%m-%d")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()]
        trading = is_market_open("cn")

        lines = [
            f"日期: {today} ({weekday})",
            f"交易日: {'是' if trading else '否'}",
        ]

        if trading:
            lines.append("交易时段: 09:30-11:30, 13:00-15:00")
        else:
            lines.append("今日休市")

        return CommandResult(ok=True, message="\n".join(lines))

    def _month(self, period: str) -> CommandResult:
        from server.calendar_service import load_trading_days

        if period:
            try:
                parts = period.split("-")
                year, month = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return CommandResult(ok=False, message=err("格式错误，使用 YYYY-MM，如 2026-06"))
        else:
            now = datetime.now()
            year, month = now.year, now.month

        try:
            trading_days = load_trading_days(year, month)
        except Exception as e:
            return CommandResult(ok=False, message=err(f"查询失败: {e}"))

        if not trading_days:
            return CommandResult(ok=True, message=f"{year}-{month:02d} 无交易日数据。")

        # 按周分组展示
        rows = []
        for d in trading_days:
            dt = datetime.strptime(d, "%Y-%m-%d")
            weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
            rows.append([d, weekday])

        table = format_table(["日期", "星期"], rows)
        return CommandResult(ok=True, message=f"{year}-{month:02d} 交易日（共 {len(trading_days)} 天）：\n{table}")

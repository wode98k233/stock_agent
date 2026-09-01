"""CLI 命令：/watchlist — 自选股管理"""
from __future__ import annotations

import sqlite3
import threading

from cli.commands.base import CommandHandler, CommandResult
from cli.context import CLIContext
from cli.output import format_table, format_json, err


class WatchlistCommand(CommandHandler):
    name = "watchlist"
    description = "自选股管理"
    usage = "/watchlist list | /watchlist quote"

    async def execute(self, ctx: CLIContext, args: list[str], flags: dict | None = None) -> CommandResult:
        action = args[0] if args else "list"

        if action == "list":
            return self._list()
        if action == "quote":
            return self._quote()

        return CommandResult(ok=False, message=err(f"未知操作: {action}"))

    def _get_storage(self):
        from server.storage import WatchlistStorage
        from utils.app_paths import get_db_path

        db_path = get_db_path()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        lock = threading.RLock()
        return WatchlistStorage(conn, lock)

    def _list(self) -> CommandResult:
        try:
            storage = self._get_storage()
            stocks = storage.list_stocks()
        except Exception as e:
            return CommandResult(ok=False, message=err(f"查询自选股失败: {e}"))

        if not stocks:
            return CommandResult(ok=True, message="自选股列表为空。使用 /watchlist add <code> 添加。")

        rows = []
        for s in stocks:
            code = s.get("stock_code", "")
            name = s.get("stock_name", "")
            price = s.get("price") or "-"
            change = s.get("change_pct") or "-"
            source = s.get("source", "")
            rows.append([code, name, str(price), str(change), source])

        table = format_table(["代码", "名称", "最新价", "涨跌幅", "来源"], rows)
        return CommandResult(ok=True, message=f"自选股（{len(stocks)} 只）：\n{table}")

    def _quote(self) -> CommandResult:
        """获取自选股实时行情。"""
        try:
            storage = self._get_storage()
            stocks = storage.list_stocks()
        except Exception as e:
            return CommandResult(ok=False, message=err(f"查询失败: {e}"))

        if not stocks:
            return CommandResult(ok=True, message="自选股列表为空。")

        codes = [s.get("stock_code", "") for s in stocks if s.get("stock_code")]
        if not codes:
            return CommandResult(ok=True, message="无有效股票代码。")

        # 使用数据源获取行情
        try:
            from server.watchlist_service import pick_source, df_to_quotes
            source = pick_source("")
            if not source:
                return CommandResult(ok=False, message=err("无可用数据源"))
        except Exception as e:
            return CommandResult(ok=False, message=err(f"获取行情失败: {e}"))

        rows = []
        for s in stocks:
            code = s.get("stock_code", "")
            name = s.get("stock_name", "")
            price = s.get("price") or "-"
            change = s.get("change_pct") or "-"
            rows.append([code, name, str(price), str(change)])

        table = format_table(["代码", "名称", "最新价", "涨跌幅"], rows)
        return CommandResult(ok=True, message=f"自选股行情：\n{table}")

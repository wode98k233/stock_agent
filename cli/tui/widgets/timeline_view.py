"""步骤时间线 — DataTable 4 列：Time / Step / Status / Detail。"""
from __future__ import annotations

from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets._data_table import ColumnKey


_STATUS_ICON: dict[str, str] = {
    "running": "⟳",
    "success": "✓",
    "failed": "✗",
    "skipped": "—",
}

_COLUMNS = ("Time", "Step", "Status", "Detail")


class TimelineView(DataTable):
    """步骤时间线。"""

    DEFAULT_CSS = """
    TimelineView {
        height: 1fr;
        background: #0d1117;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._step_keys: dict[str, RowKey] = {}
        self._col_keys: list[ColumnKey] = []

    def on_mount(self) -> None:
        self._col_keys = self.add_columns(*_COLUMNS)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def add_step(self, step: str, status: str, elapsed: str, detail: str) -> None:
        icon = _STATUS_ICON.get(status, " ")
        detail_trunc = detail[:50] if len(detail) > 50 else detail
        row_key = self.add_row(elapsed, step, f"{icon} {status}", detail_trunc)
        self._step_keys[step] = row_key

    def update_step_status(self, step: str, status: str) -> None:
        row_key = self._step_keys.get(step)
        if row_key is None:
            return
        icon = _STATUS_ICON.get(status, " ")
        status_col = self._col_keys[2] if len(self._col_keys) > 2 else ColumnKey("Status")
        self.update_cell(row_key, status_col, f"{icon} {status}")

    def clear_steps(self) -> None:
        self.clear(columns=False)
        self._step_keys.clear()

    def find_step(self, step: str) -> int | None:
        return self._step_keys.get(step)

    def get_row_count(self) -> int:
        return self.row_count

    def cell_at(self, coordinate: Coordinate) -> str:
        try:
            value = self.get_cell_at(coordinate)
        except Exception:
            return ""
        return str(value) if value is not None else ""

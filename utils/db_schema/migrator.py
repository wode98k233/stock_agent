"""diff 引擎：缺表建、缺列补、缺索引建。

无版本号、加性幂等、不删数据。设计见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md
"""
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from utils.db_schema.defs import TableDef


@dataclass
class ChangeReport:
    created: list[str] = field(default_factory=list)
    added_columns: dict[str, list[str]] = field(default_factory=dict)
    migrations: list[str] = field(default_factory=list)


# 复合约束/关键字行不属于"列定义"，跳过
_SKIP_COL_LINE = re.compile(
    r"^\s*(PRIMARY|UNIQUE|FOREIGN|CHECK|CONSTRAINT)\b", re.IGNORECASE)


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    """按 sep 分割，忽略括号内的 sep。"""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _extract_columns(ddl: str) -> list[str]:
    """从 CREATE TABLE 文本解析顶层列名（单行/多行均支持，忽略复合约束行）。"""
    start = ddl.find("(")
    end = ddl.rfind(")")
    if start == -1 or end == -1 or end < start:
        return []
    cols = []
    for part in _split_top_level(ddl[start + 1:end]):
        s = part.strip()
        if not s or _SKIP_COL_LINE.match(s):
            continue
        m = re.match(r"^([A-Za-z_]\w*)\s", s)
        if m:
            cols.append(m.group(1))
    return cols


def _table_info(conn, name: str) -> dict[str, str] | None:
    rows = conn.execute("PRAGMA table_info(%s)" % name).fetchall()
    if not rows:
        return None
    return {r[1]: r[2] for r in rows}


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master"
        " WHERE type IN ('table','view') AND name = ? LIMIT 1",
        (name,)).fetchone()
    return row is not None


def _col_def(ddl: str, col: str) -> str:
    """从 DDL 原文取某列的定义片段（ALTER TABLE ADD COLUMN 用）。"""
    for line in ddl.splitlines():
        m = re.match(r"^\s*([A-Za-z_]\w*)\s+(.*)$", line)
        if m and m.group(1) == col:
            return f"{m.group(1)} {m.group(2).rstrip(',')}"
    raise ValueError(f"列定义未找到: {col}（column={col} ddl={ddl!r}）")


def diff_schema(
    conn: sqlite3.Connection,
    table_defs: Iterable[TableDef],
) -> ChangeReport:
    """只读 diff：报告将发生的变更，不执行任何写语句（--check 用）。"""
    report = ChangeReport()
    for td in table_defs:
        if td.virtual:
            if not _table_exists(conn, td.name):
                report.created.append(td.name)
            continue
        current = _table_info(conn, td.name)
        if current is None:
            report.created.append(td.name)
            continue
        missing = [c for c in _extract_columns(td.ddl) if c not in current]
        if missing:
            report.added_columns[td.name] = missing
    return report


def ensure_schema(
    conn: sqlite3.Connection,
    table_defs: Iterable[TableDef],
    special_migrations: Iterable[Callable[[sqlite3.Connection], None]] = (),
) -> ChangeReport:
    """把 conn 指向的库对齐到 table_defs 最新定义。幂等、只做加性操作。"""
    report = ChangeReport()
    for td in table_defs:
        if td.virtual:
            if not _table_exists(conn, td.name):
                conn.executescript(td.ddl)
                report.created.append(td.name)
            continue
        current = _table_info(conn, td.name)
        if current is None:
            sql = ";\n".join([td.ddl] + (td.indexes or [])) + ";"
            conn.executescript(sql)
            report.created.append(td.name)
            continue
        want = _extract_columns(td.ddl)
        missing = [c for c in want if c not in current]
        for c in missing:
            conn.execute(
                f"ALTER TABLE {td.name} ADD COLUMN {_col_def(td.ddl, c)}")
            report.added_columns.setdefault(td.name, []).append(c)
    for td in table_defs:
        for idx in td.indexes or []:
            conn.execute(idx)  # CREATE INDEX IF NOT EXISTS 幂等
    for m in special_migrations:
        m(conn)
        report.migrations.append(getattr(m, "__name__", str(m)))
    conn.commit()
    return report
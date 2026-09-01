"""CLI 输出格式化 — 表格、JSON、Markdown、状态行

复用 cli/banner.py 的 CJK 宽度计算。
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any


# ── CJK 宽度计算（从 banner.py 复用） ──────────────────────


def display_width(text: str) -> int:
    """计算字符串的显示宽度（CJK 字符占 2 列）。"""
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width


def pad_right(text: str, width: int) -> str:
    """右填充到指定显示宽度。"""
    return text + " " * max(0, width - display_width(text))


# ── 表格输出 ──────────────────────────────────────────────


def format_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    max_col_width: int = 80,
) -> str:
    """渲染轻量 ASCII 表格，支持中文对齐。"""
    if not headers:
        return ""

    # 计算每列宽度
    col_widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], display_width(cell[:max_col_width]))

    # 截断过宽列
    col_widths = [min(w, max_col_width) for w in col_widths]

    # 构建行
    def _fmt_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            w = col_widths[i] if i < len(col_widths) else 10
            parts.append(pad_right(cell[:max_col_width], w))
        return "  ".join(parts)

    def _sep() -> str:
        return "  ".join("-" * w for w in col_widths)

    lines = [_fmt_row(headers), _sep()]
    for row in rows:
        lines.append(_fmt_row(row))
    return "\n".join(lines)


# ── JSON 输出 ─────────────────────────────────────────────


def format_json(data: Any, *, pretty: bool = True) -> str:
    """格式化 JSON 输出。"""
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return json.dumps(data, ensure_ascii=False)


# ── 状态行 ────────────────────────────────────────────────


def status_line(label: str, value: str, *, width: int = 60) -> str:
    """渲染一行状态信息：label ........ value"""
    dots = "." * max(2, width - display_width(label) - display_width(value))
    return f"{label}  {dots}  {value}"


# ── 分隔线 ────────────────────────────────────────────────


def separator(char: str = "─", width: int = 60) -> str:
    return char * width


# ── 错误/成功前缀 ─────────────────────────────────────────


def ok(text: str) -> str:
    return f"[OK] {text}"


def err(text: str) -> str:
    return f"[ERROR] {text}"


def warn(text: str) -> str:
    return f"[WARN] {text}"


def info(text: str) -> str:
    return f"[INFO] {text}"

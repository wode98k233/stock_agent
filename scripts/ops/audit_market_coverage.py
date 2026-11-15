"""A 股行情数据覆盖审计（只读，不写库、不联网）。

背景：策略研究受限于 `db/market_data.db` 的 `stock_daily` 历史长度。
本脚本只回答「库里到底有多少数据」，不做任何补数或修复：

- 总行数 / 标的数 / 平均根数；
- 根数分桶：1~30 / 31~120 / 121~250 / 251~399 / >=400；
- 指定窗口内 >=400 根的标的数（默认 2025-01-01 ~ 2026-10-20）；
- 字段完整率：pre_close / pct_change / change_amount / amount /
  turnover_rate / amplitude（全库、窗口各一份）；
- 可选 --top N：根数最少 / 最多的 N 只；
- 可选 --codes：单只诊断（区间、周末行、close-only 行、字段完整率、数据源）；
- 附加数据卫生提示（周末行、非 6 位代码、close 为空），只提示不改。

用法：
    python scripts/ops/audit_market_coverage.py
    python scripts/ops/audit_market_coverage.py --top 10
    python scripts/ops/audit_market_coverage.py --codes 600519,000001
    python scripts/ops/audit_market_coverage.py --start 2025-01-01 --end 2026-10-20
    python scripts/ops/audit_market_coverage.py --json /tmp/coverage.json

Windows 终端如中文乱码，先设置 `$env:PYTHONIOENCODING='utf-8'`。

口径：
- 只读连接使用 SQLite URI `mode=ro`，并额外 `PRAGMA query_only=ON`；
- 所有 SQL 只有 SELECT / PRAGMA，没有 CREATE / INSERT / UPDATE / DELETE；
- 纯函数（分桶、完整率、日期过滤）与 IO 分离，便于单测。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "db" / "market_data.db"
DEFAULT_START = "2025-01-01"
DEFAULT_END = "2026-10-20"

# 字段顺序即输出顺序，避免不同 Python / SQLite 版本的字典顺序差异
FIELDS: tuple[str, ...] = (
    "pre_close",
    "pct_change",
    "change_amount",
    "amount",
    "turnover_rate",
    "amplitude",
)

# (下限, 上限 or None, 展示标签)
BUCKETS: tuple[tuple[int, int | None, str], ...] = (
    (1, 30, "1~30"),
    (31, 120, "31~120"),
    (121, 250, "121~250"),
    (251, 399, "251~399"),
    (400, None, "≥400"),
)

QUALIFIED_BARS = 400
_SIX_DIGIT_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9]"
_WEEKEND_SQL = "strftime('%w', trade_date) IN ('0', '6')"
_NON_SIX_DIGIT_SQL = f"NOT (length(code) = 6 AND code GLOB '{_SIX_DIGIT_GLOB}')"


# ---------------------------------------------------------------------------
# 纯函数（无 IO，可直接单测）
# ---------------------------------------------------------------------------
def bucket_of(bars: int) -> str:
    """返回根数所属分桶标签；1 根起，0 或负数视为无效输入。"""
    if bars < 1:
        raise ValueError(f"bars 必须 >= 1，收到 {bars!r}")
    for lower, upper, label in BUCKETS:
        if bars >= lower and (upper is None or bars <= upper):
            return label
    raise AssertionError(f"分桶未覆盖 {bars}")  # 理论上不可达


def bucket_stats(bar_counts: Iterable[int]) -> list[dict]:
    """把每只标的的根数聚合成分桶统计（标的数 + 占比）。"""
    counts = {label: 0 for _, _, label in BUCKETS}
    for bars in bar_counts:
        counts[bucket_of(int(bars))] += 1
    total = sum(counts.values())
    rows = []
    for lower, upper, label in BUCKETS:
        n = counts[label]
        rows.append(
            {
                "label": label,
                "min_bars": lower,
                "max_bars": upper,
                "codes": n,
                "pct": round(n * 100.0 / total, 2) if total else None,
            }
        )
    return rows


def completeness_rates(
    non_null_counts: Mapping[str, int],
    total_rows: int,
    fields: Sequence[str] = FIELDS,
) -> dict[str, float | None]:
    """非空率（0~1）；分母为 0 时返回 None，表示「无从计算」而不是 0。"""
    if total_rows <= 0:
        return {field: None for field in fields}
    return {
        field: round(int(non_null_counts.get(field, 0)) / float(total_rows), 6)
        for field in fields
    }


def summarize_bar_counts(bar_counts: Mapping[str, int]) -> dict:
    """总行数 / 标的数 / 平均根数。"""
    rows = int(sum(bar_counts.values()))
    codes = len(bar_counts)
    return {
        "rows": rows,
        "codes": codes,
        "avg_bars": round(rows / codes, 2) if codes else None,
    }


def parse_codes(text: str) -> list[str]:
    """解析 --codes：逗号或分号分隔，去空白、去重且保持输入顺序。"""
    result: list[str] = []
    for raw in (text or "").replace(";", ",").split(","):
        code = raw.strip()
        if code and code not in result:
            result.append(code)
    return result


def parse_iso_date(text: str) -> date:
    """严格解析 YYYY-MM-DD，供 argparse 使用。"""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是合法日期（YYYY-MM-DD）：{text}") from exc


def is_weekend(date_text: str) -> bool:
    """ISO 日期是否为周六/周日（纯函数，供单测和诊断兜底）。"""
    return date.fromisoformat(date_text).weekday() >= 5


# ---------------------------------------------------------------------------
# 展示辅助（纯函数）
# ---------------------------------------------------------------------------
def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _display_width(str(text)))
    if align == "right":
        return " " * gap + str(text)
    return str(text) + " " * gap


def _fmt_int(value: int | None) -> str:
    return "n/a" if value is None else f"{int(value):,}"


def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.2f}%"


def _fmt_avg(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _join_codes(items: list[dict], limit: int = 30) -> str:
    shown = items[:limit]
    text = ", ".join(f"{item['code']}({item['bars']})" for item in shown)
    if len(items) > limit:
        text += f" ... 另 {len(items) - limit} 只"
    return text or "无"


# ---------------------------------------------------------------------------
# IO（只读）
# ---------------------------------------------------------------------------
def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """以 SQLite URI mode=ro 打开，并加 query_only 双保险。"""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def ensure_stock_daily(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_daily'"
    ).fetchone()
    if row is None:
        raise RuntimeError("数据库里没有 stock_daily 表")


def _date_filter(start: str | None, end: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start:
        clauses.append("trade_date >= ?")
        params.append(start)
    if end:
        clauses.append("trade_date <= ?")
        params.append(end)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def load_bar_counts(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None
) -> dict[str, int]:
    where, params = _date_filter(start, end)
    sql = f"SELECT code, COUNT(*) AS bars FROM stock_daily{where} GROUP BY code ORDER BY code"
    return {str(row["code"]): int(row["bars"]) for row in conn.execute(sql, params)}


def load_completeness(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None
) -> dict:
    where, params = _date_filter(start, end)
    selects = ", ".join(f"COUNT({field}) AS {field}" for field in FIELDS)
    row = conn.execute(f"SELECT COUNT(*) AS rows, {selects} FROM stock_daily{where}", params).fetchone()
    total = int(row["rows"])
    non_null = {field: int(row[field]) for field in FIELDS}
    return {
        "rows": total,
        "non_null": non_null,
        "rates": completeness_rates(non_null, total),
    }


def load_sources(conn: sqlite3.Connection) -> list[dict]:
    selects = ", ".join(f"COUNT({field}) AS {field}" for field in FIELDS)
    sql = (
        "SELECT source, COUNT(*) AS rows, COUNT(DISTINCT code) AS codes, "
        f"{selects} FROM stock_daily GROUP BY source ORDER BY rows DESC, source"
    )
    result = []
    for row in conn.execute(sql):
        total = int(row["rows"])
        non_null = {field: int(row[field]) for field in FIELDS}
        result.append(
            {
                "source": row["source"] or "未知",
                "rows": total,
                "codes": int(row["codes"]),
                "non_null": non_null,
                "rates": completeness_rates(non_null, total),
            }
        )
    return result


def load_diagnostic(
    conn: sqlite3.Connection, code: str, start: str | None = None, end: str | None = None
) -> dict:
    selects = ", ".join(f"COUNT({field}) AS {field}" for field in FIELDS)
    row = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT trade_date) AS trade_dates, "
        "MIN(trade_date) AS first_date, MAX(trade_date) AS last_date, "
        f"{selects}, "
        f"COALESCE(SUM(CASE WHEN {_WEEKEND_SQL} THEN 1 ELSE 0 END), 0) AS weekend_rows, "
        "COALESCE(SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END), 0) AS close_null_rows, "
        "COALESCE(SUM(CASE WHEN close IS NOT NULL AND open IS NULL AND high IS NULL "
        "AND low IS NULL AND volume IS NULL AND amount IS NULL THEN 1 ELSE 0 END), 0) "
        "AS close_only_rows "
        "FROM stock_daily WHERE code = ?",
        (code,),
    ).fetchone()

    total = int(row["rows"])
    non_null = {field: int(row[field]) for field in FIELDS}

    where, params = _date_filter(start, end)
    window_sql = "SELECT COUNT(*) AS n FROM stock_daily WHERE code = ?"
    if where:
        window_sql += " AND " + where[len(" WHERE ") :]
    window_rows = int(conn.execute(window_sql, [code, *params]).fetchone()["n"])

    sources = [
        {"source": src["source"] or "未知", "rows": int(src["n"])}
        for src in conn.execute(
            "SELECT source, COUNT(*) AS n FROM stock_daily WHERE code = ? "
            "GROUP BY source ORDER BY n DESC, source",
            (code,),
        )
    ]

    return {
        "code": code,
        "rows": total,
        "window_rows": window_rows,
        "trade_dates": int(row["trade_dates"]),
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "weekend_rows": int(row["weekend_rows"]),
        "close_null_rows": int(row["close_null_rows"]),
        "close_only_rows": int(row["close_only_rows"]),
        "non_null": non_null,
        "rates": completeness_rates(non_null, total),
        "sources": sources,
    }


def load_hygiene(conn: sqlite3.Connection) -> dict:
    weekend = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes "
        f"FROM stock_daily WHERE {_WEEKEND_SQL}"
    ).fetchone()
    weekend_std = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes "
        f"FROM stock_daily WHERE {_WEEKEND_SQL} "
        f"AND length(code) = 6 AND code GLOB '{_SIX_DIGIT_GLOB}'"
    ).fetchone()
    weekend_codes = [
        {"code": str(r["code"]), "rows": int(r["rows"])}
        for r in conn.execute(
            "SELECT code, COUNT(*) AS rows FROM stock_daily "
            f"WHERE {_WEEKEND_SQL} AND length(code) = 6 "
            f"AND code GLOB '{_SIX_DIGIT_GLOB}' "
            "GROUP BY code ORDER BY rows DESC, code LIMIT 10"
        )
    ]
    non_std = conn.execute(
        f"SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes "
        f"FROM stock_daily WHERE {_NON_SIX_DIGIT_SQL}"
    ).fetchone()
    close_null = int(
        conn.execute("SELECT COUNT(*) AS n FROM stock_daily WHERE close IS NULL").fetchone()["n"]
    )
    span = conn.execute(
        "SELECT MIN(trade_date) AS first_date, MAX(trade_date) AS last_date, "
        "COUNT(DISTINCT trade_date) AS trade_dates FROM stock_daily"
    ).fetchone()
    return {
        "weekend_rows": int(weekend["rows"]),
        "weekend_codes": int(weekend["codes"]),
        "weekend_std_rows": int(weekend_std["rows"]),
        "weekend_std_codes": int(weekend_std["codes"]),
        "weekend_code_list": weekend_codes,
        "non_standard_rows": int(non_std["rows"]),
        "non_standard_codes": int(non_std["codes"]),
        "close_null_rows": close_null,
        "first_date": span["first_date"],
        "last_date": span["last_date"],
        "trade_dates": int(span["trade_dates"]),
    }


# ---------------------------------------------------------------------------
# 组装审计结果
# ---------------------------------------------------------------------------
def build_audit(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    top_n: int = 0,
    codes: Iterable[str] = (),
    db_path: str | Path | None = None,
    generated_at: str | None = None,
) -> dict:
    ensure_stock_daily(conn)

    full_counts = load_bar_counts(conn)
    window_counts = load_bar_counts(conn, start, end)
    overview = summarize_bar_counts(full_counts)
    window_summary = summarize_bar_counts(window_counts)

    ge_400 = [
        {"code": code, "bars": bars}
        for code, bars in window_counts.items()
        if bars >= QUALIFIED_BARS
    ]
    ge_400.sort(key=lambda item: (-item["bars"], item["code"]))

    top_items = sorted(full_counts.items(), key=lambda item: (item[1], item[0]))
    fewest = [{"code": code, "bars": bars} for code, bars in top_items[:top_n]] if top_n > 0 else []
    most_sorted = sorted(full_counts.items(), key=lambda item: (-item[1], item[0]))
    most = [{"code": code, "bars": bars} for code, bars in most_sorted[:top_n]] if top_n > 0 else []

    requested_codes = parse_codes(codes if isinstance(codes, str) else ",".join(codes))

    return {
        "db_path": str(Path(db_path).resolve()) if db_path else None,
        "generated_at": generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "read_only": True,
        "window": {
            "start": start,
            "end": end,
            "rows": window_summary["rows"],
            "codes": window_summary["codes"],
            "avg_bars": window_summary["avg_bars"],
            "qualified_bars": QUALIFIED_BARS,
            "ge_qualified_count": len(ge_400),
            "ge_qualified_codes": ge_400,
        },
        "overview": overview,
        "buckets": bucket_stats(full_counts.values()),
        "completeness": {
            "all": load_completeness(conn),
            "window": load_completeness(conn, start, end),
        },
        "top": {"n": top_n, "fewest": fewest, "most": most},
        "diagnostics": [
            load_diagnostic(conn, code, start, end) for code in requested_codes
        ],
        "sources": load_sources(conn),
        "hygiene": load_hygiene(conn),
    }


# ---------------------------------------------------------------------------
# 文本渲染
# ---------------------------------------------------------------------------
def render_text(audit: dict) -> str:
    lines: list[str] = []
    sep = "=" * 78
    sub = "-" * 78

    lines += [
        sep,
        "A 股行情数据覆盖审计（只读）",
        sep,
        f"数据库      : {audit.get('db_path') or 'n/a'}",
        f"审计窗口    : {audit['window']['start']} ~ {audit['window']['end']}",
        f"生成时间    : {audit['generated_at']}",
        "审计口径    : 只读打开（mode=ro + query_only），不写库、不联网、不补数",
        sub,
    ]

    overview = audit["overview"]
    lines += [
        "一、总览",
        f"  {_pad('总行数', 20)}{_fmt_int(overview['rows'])}",
        f"  {_pad('标的数', 20)}{_fmt_int(overview['codes'])}",
        f"  {_pad('平均每标的根数', 20)}{_fmt_avg(overview['avg_bars'])}",
        "",
    ]

    lines += [
        "二、根数分桶（全库）",
        f"  {_pad('区间', 10)}{_pad('标的数', 12, 'right')}{_pad('占比', 10, 'right')}",
    ]
    for bucket in audit["buckets"]:
        pct = "n/a" if bucket["pct"] is None else f"{bucket['pct']:.2f}%"
        lines.append(
            f"  {_pad(bucket['label'], 10)}"
            f"{_pad(_fmt_int(bucket['codes']), 12, 'right')}"
            f"{_pad(pct, 10, 'right')}"
        )
    lines.append("")

    window = audit["window"]
    qualified_label = f"≥{window['qualified_bars']} 根标的数"
    lines += [
        f"三、窗口内可用标的（{window['start']} ~ {window['end']}）",
        f"  {_pad('窗口内行数', 20)}{_fmt_int(window['rows'])}",
        f"  {_pad('窗口内标的数', 20)}{_fmt_int(window['codes'])}",
        f"  {_pad('窗口内平均根数', 20)}{_fmt_avg(window['avg_bars'])}",
        f"  {_pad(qualified_label, 20)}{_fmt_int(window['ge_qualified_count'])}",
    ]
    if window["ge_qualified_count"]:
        lines.append("  ≥400 根标的        " + _join_codes(window["ge_qualified_codes"]))
    else:
        lines.append("  ≥400 根标的        无")
    lines.append("")

    all_comp = audit["completeness"]["all"]
    win_comp = audit["completeness"]["window"]
    lines += [
        "四、字段完整率（非空 / 总行数）",
        f"  {_pad('字段', 18)}{_pad('全库非空', 12, 'right')}"
        f"{_pad('全库完整率', 12, 'right')}{_pad('窗口非空', 12, 'right')}"
        f"{_pad('窗口完整率', 12, 'right')}",
    ]
    for field in FIELDS:
        lines.append(
            f"  {_pad(field, 18)}"
            f"{_pad(_fmt_int(all_comp['non_null'][field]), 12, 'right')}"
            f"{_pad(_fmt_rate(all_comp['rates'][field]), 12, 'right')}"
            f"{_pad(_fmt_int(win_comp['non_null'][field]), 12, 'right')}"
            f"{_pad(_fmt_rate(win_comp['rates'][field]), 12, 'right')}"
        )
    lines.append(
        f"  分母：全库 {_fmt_int(all_comp['rows'])} 行；窗口 {_fmt_int(win_comp['rows'])} 行"
    )
    lines.append("")

    top = audit["top"]
    if top["n"] > 0:
        lines.append(f"五、根数最少 / 最多（各 {top['n']} 只）")
        lines.append(f"  最少 {top['n']} 只")
        for item in top["fewest"]:
            lines.append(f"    {_pad(item['code'], 12)}{_pad(_fmt_int(item['bars']), 8, 'right')}")
        lines.append(f"  最多 {top['n']} 只")
        for item in top["most"]:
            lines.append(f"    {_pad(item['code'], 12)}{_pad(_fmt_int(item['bars']), 8, 'right')}")
        lines.append("")
    else:
        lines += ["五、根数最少 / 最多（未指定 --top，跳过）", ""]

    if audit["diagnostics"]:
        lines.append("六、单只诊断")
        for diag in audit["diagnostics"]:
            lines.append(f"  [{diag['code']}]")
            if diag["rows"] == 0:
                lines.append("    库中无数据（0 行）")
                lines.append("")
                continue
            lines += [
                f"    {_pad('根数', 18)}: {_fmt_int(diag['rows'])}",
                f"    {_pad('窗口内根数', 18)}: {_fmt_int(diag['window_rows'])}",
                f"    {_pad('数据区间', 18)}: {diag['first_date']} ~ {diag['last_date']}",
                f"    {_pad('去重交易日', 18)}: {_fmt_int(diag['trade_dates'])}",
                f"    {_pad('周末行数', 18)}: {_fmt_int(diag['weekend_rows'])}",
                f"    {_pad('close 为空行数', 18)}: {_fmt_int(diag['close_null_rows'])}",
                f"    {_pad('close-only 行数', 18)}: {_fmt_int(diag['close_only_rows'])}",
                "    数据源            : "
                + (", ".join(f"{s['source']}({s['rows']})" for s in diag["sources"]) or "无"),
                "    字段完整率        :",
            ]
            for field in FIELDS:
                lines.append(
                    f"      {_pad(field, 18)}"
                    f"{_pad(_fmt_int(diag['non_null'][field]), 10, 'right')}"
                    f"{_pad(_fmt_rate(diag['rates'][field]), 10, 'right')}"
                )
            lines.append("")
    else:
        lines += ["六、单只诊断（未指定 --codes，跳过）", ""]

    lines.append("七、数据源分布（解释字段完整率）")
    lines.append(
        f"  {_pad('source', 16)}{_pad('行数', 11, 'right')}{_pad('标的数', 9, 'right')}"
        f"{_pad('pre_close', 12, 'right')}{_pad('change_amount', 15, 'right')}"
        f"{_pad('turnover_rate', 14, 'right')}"
    )
    for src in audit["sources"]:
        lines.append(
            f"  {_pad(src['source'], 16)}"
            f"{_pad(_fmt_int(src['rows']), 11, 'right')}"
            f"{_pad(_fmt_int(src['codes']), 9, 'right')}"
            f"{_pad(_fmt_rate(src['rates']['pre_close']), 12, 'right')}"
            f"{_pad(_fmt_rate(src['rates']['change_amount']), 15, 'right')}"
            f"{_pad(_fmt_rate(src['rates']['turnover_rate']), 14, 'right')}"
        )
    lines.append("")

    hygiene = audit["hygiene"]
    lines += [
        "八、数据卫生提示（只读，不改）",
        f"  {_pad('周末（周六/周日）行数', 24)}: {_fmt_int(hygiene['weekend_rows'])}"
        f"（涉及 {_fmt_int(hygiene['weekend_codes'])} 个标的）",
        f"  {_pad('其中六位代码周末行', 24)}: {_fmt_int(hygiene['weekend_std_rows'])}"
        f"（涉及 {_fmt_int(hygiene['weekend_std_codes'])} 个标的）",
    ]
    if hygiene["weekend_code_list"]:
        lines.append(
            "    六位代码周末行明细  : "
            + ", ".join(f"{x['code']}({x['rows']})" for x in hygiene["weekend_code_list"])
        )
    lines += [
        f"  {_pad('非 6 位数字代码', 24)}: {_fmt_int(hygiene['non_standard_rows'])} 行"
        f" / {_fmt_int(hygiene['non_standard_codes'])} 个代码",
        f"  {_pad('close 为空的行数', 24)}: {_fmt_int(hygiene['close_null_rows'])}",
        f"  {_pad('最早/最晚交易日', 24)}: {hygiene['first_date']} / {hygiene['last_date']}"
        f"（{_fmt_int(hygiene['trade_dates'])} 个交易日）",
        sep,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要整数：{text}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"需要 >= 0 的整数：{text}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A 股行情数据覆盖审计（只读，不写库、不联网）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="market_data.db 路径")
    parser.add_argument("--start", default=DEFAULT_START, type=parse_iso_date, help="窗口开始日")
    parser.add_argument("--end", default=DEFAULT_END, type=parse_iso_date, help="窗口结束日")
    parser.add_argument("--top", default=0, type=_non_negative_int, help="输出根数最少/最多的各 N 只")
    parser.add_argument("--codes", default="", help="单只诊断，逗号分隔，如 600519,000001")
    parser.add_argument("--json", dest="json_path", default="", help="把完整结果另存为 JSON 文件")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error(f"--start ({args.start}) 不能晚于 --end ({args.end})")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"错误：数据库不存在：{db_path}", file=sys.stderr)
        return 2

    try:
        conn = connect_readonly(db_path)
    except sqlite3.Error as exc:
        print(f"错误：无法只读打开数据库：{exc}", file=sys.stderr)
        return 2

    try:
        audit = build_audit(
            conn,
            start=args.start.isoformat(),
            end=args.end.isoformat(),
            top_n=args.top,
            codes=parse_codes(args.codes),
            db_path=db_path,
        )
    except (RuntimeError, sqlite3.Error) as exc:
        print(f"错误：审计失败：{exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(render_text(audit))

    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整结果已写入：{out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

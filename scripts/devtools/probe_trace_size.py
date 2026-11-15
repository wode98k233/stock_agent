"""Trace 存储体积探针（只读）。

回答三个问题，全部用**实测字节**而不是估算：

1. 体积到底花在哪张表、哪个字段？
2. 重复度有多高（内容寻址 + 压缩的"理论下限"能给到多少）？
3. 瘦身改造（T1 摘要化 / T2 内容寻址）后应该落在什么量级？

⚠️ 口径铁律：**必须用 ``length(cast(x as blob))`` 取 UTF-8 字节**。
SQLite 的 ``length(TEXT)`` 返回**字符数**，中文占 3 字节，直接用它会把体积
低估 2.6~3 倍（本仓 2026-10-19 那次盘点就先踩过这个坑）。

用法::

    python scripts/devtools/probe_trace_size.py                  # 全部月库汇总
    python scripts/devtools/probe_trace_size.py --month 202610    # 单月明细
    python scripts/devtools/probe_trace_size.py --no-compress     # 跳过压缩估算（快）
    python scripts/devtools/probe_trace_size.py --json out.json   # 机器可读

设计：docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 行级大字段（决定体积的就是这些）
_TEXT_COLUMNS = (
    ("steps", "input"),
    ("steps", "output"),
    ("steps", "extra"),
    ("messages", "content"),
    ("messages", "reasoning"),
    ("messages", "tool_calls"),
    ("runs", "input"),
    ("runs", "output"),
)
_CHUNK = 4096
# 瘦身目标模型：只保留 messages + steps.output（去掉全部 steps.input）
_TARGET_MODEL = (
    ("messages", "content"),
    ("messages", "reasoning"),
    ("steps", "output"),
    ("runs", "output"),
)


def _fmt(n: float) -> str:
    return f"{n / 1048576:.1f} MB"


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.text_factory = bytes
    return conn


def _bytes_of(conn: sqlite3.Connection, table: str, column: str) -> int:
    try:
        row = conn.execute(
            f"SELECT coalesce(sum(length(cast({column} AS blob))), 0) FROM {table}"
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    try:
        return int(conn.execute(sql).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def _dedup_estimate(conn: sqlite3.Connection, columns, *, compress: bool):
    """4KB 块内容寻址 → 去重字节；再 zlib(6) → 压缩字节。"""
    seen: dict[bytes, bytes] = {}
    raw = 0
    for table, column in columns:
        try:
            cur = conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
        except sqlite3.Error:
            continue
        for (blob,) in cur:
            if not isinstance(blob, (bytes, bytearray)):
                blob = str(blob).encode("utf-8", "replace")
            raw += len(blob)
            for i in range(0, len(blob), _CHUNK):
                chunk = blob[i:i + _CHUNK]
                key = hashlib.blake2b(chunk, digest_size=8).digest()
                if key not in seen:
                    seen[key] = chunk
    unique = sum(len(v) for v in seen.values())
    compressed = sum(len(zlib.compress(v, 6)) for v in seen.values()) if compress else 0
    return {"raw": raw, "unique": unique, "compressed": compressed, "chunks": len(seen)}


def probe_month(db: Path, *, compress: bool = True) -> dict:
    conn = _connect(db)
    try:
        info = {
            "db": db.name,
            "file_bytes": db.stat().st_size,
            "page_size": _scalar(conn, "PRAGMA page_size"),
            "page_count": _scalar(conn, "PRAGMA page_count"),
            "freelist_pages": _scalar(conn, "PRAGMA freelist_count"),
            "runs": _scalar(conn, "SELECT COUNT(*) FROM runs"),
            "steps": _scalar(conn, "SELECT COUNT(*) FROM steps"),
            "messages": _scalar(conn, "SELECT COUNT(*) FROM messages"),
            "columns": {},
            "step_types": [],
            "roles": [],
        }
        info["pages_bytes"] = info["page_size"] * info["page_count"]
        for table, column in _TEXT_COLUMNS:
            info["columns"][f"{table}.{column}"] = _bytes_of(conn, table, column)

        text_total = sum(info["columns"].values())
        info["text_bytes"] = text_total
        info["text_share"] = (text_total / info["file_bytes"]) if info["file_bytes"] else 0.0

        for step_type, count, in_b, out_b in conn.execute(
            "SELECT step_type, COUNT(*), "
            "coalesce(sum(length(cast(coalesce(input,'') AS blob))),0), "
            "coalesce(sum(length(cast(coalesce(output,'') AS blob))),0) "
            "FROM steps GROUP BY step_type ORDER BY 3 DESC LIMIT 10"
        ):
            info["step_types"].append({
                "step_type": (step_type or b"").decode("utf-8", "replace") if isinstance(step_type, bytes) else step_type,
                "count": count, "input_bytes": in_b, "output_bytes": out_b,
            })

        for role, count, total in conn.execute(
            "SELECT role, COUNT(*), coalesce(sum(length(cast(coalesce(content,'') AS blob))),0) "
            "FROM messages GROUP BY role ORDER BY 3 DESC"
        ):
            # text_factory=bytes 时 role 是 bytes，直接绑回 WHERE 会按 BLOB 比较 → 永远 0 行
            role_text = role.decode("utf-8", "replace") if isinstance(role, (bytes, bytearray)) else str(role)
            distinct = conn.execute(
                "SELECT COUNT(DISTINCT content) FROM messages WHERE role=?", (role_text,)
            ).fetchone()[0]
            unique_bytes = conn.execute(
                "SELECT coalesce(sum(length(cast(content AS blob))),0) FROM "
                "(SELECT DISTINCT content FROM messages WHERE role=?)", (role_text,)
            ).fetchone()[0]
            info["roles"].append({
                "role": role_text,
                "rows": count, "bytes": total, "distinct": int(distinct or 0),
                "unique_bytes": int(unique_bytes or 0),
                "dup_ratio": round(1 - (distinct or 0) / count, 3) if count else 0.0,
                "byte_dup_ratio": round(1 - (unique_bytes or 0) / total, 3) if total else 0.0,
            })

        info["all_text"] = _dedup_estimate(conn, _TEXT_COLUMNS, compress=compress)
        info["target_model"] = _dedup_estimate(conn, _TARGET_MODEL, compress=compress)
        return info
    finally:
        conn.close()


def _print_month(info: dict, *, compress: bool) -> None:
    print(f"\n=== {info['db']} ===")
    print(f"文件 {_fmt(info['file_bytes'])} · 页 {info['page_count']}×{info['page_size']}"
          f"（{_fmt(info['pages_bytes'])}）· freelist {info['freelist_pages']} 页")
    print(f"runs={info['runs']} steps={info['steps']} messages={info['messages']}")
    print(f"文本合计 {_fmt(info['text_bytes'])}，占文件 {info['text_share'] * 100:.1f}%"
          f" —— 索引/元数据仅 {_fmt(info['file_bytes'] - info['text_bytes'])}")

    print("  ── 字段账单 ──")
    for key, value in sorted(info["columns"].items(), key=lambda kv: -kv[1]):
        if value:
            share = value / info["text_bytes"] * 100 if info["text_bytes"] else 0
            print(f"    {key:22s} {_fmt(value):>10s}  {share:5.1f}%")

    print("  ── 按 step_type（input/output）──")
    for row in info["step_types"]:
        print(f"    {row['step_type']:20s} n={row['count']:5d}"
              f"  in={_fmt(row['input_bytes']):>9s}  out={_fmt(row['output_bytes']):>9s}")

    print("  ── messages 重复度（按 role）──")
    for row in info["roles"]:
        print(f"    {row['role']:8s} rows={row['rows']:5d} {_fmt(row['bytes']):>9s}"
              f"  不同内容={row['distinct']:5d}"
              f"  重复率(按行)={row['dup_ratio'] * 100:4.0f}%"
              f"  去重后={_fmt(row['unique_bytes']):>9s}"
              f"（按字节重复 {row['byte_dup_ratio'] * 100:4.0f}%）")

    all_text = info["all_text"]
    if all_text["raw"]:
        print(f"  ── 理论下限（4KB 块内容寻址）──")
        print(f"    全库文本 {_fmt(all_text['raw'])} → 去重 {_fmt(all_text['unique'])}"
              f"（{all_text['unique'] / all_text['raw'] * 100:.0f}%）"
              + (f" → +zlib {_fmt(all_text['compressed'])}"
                 f"（{all_text['raw'] / all_text['compressed']:.1f}x）" if compress else ""))
        target = info["target_model"]
        print(f"    目标模型(messages+steps.output) {_fmt(target['raw'])} → 去重 {_fmt(target['unique'])}"
              + (f" → +zlib {_fmt(target['compressed'])}" if compress else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Trace 存储体积探针（只读）")
    parser.add_argument("--month", default="", help="只探某个月库，如 202610")
    parser.add_argument("--no-compress", action="store_true", help="跳过 zlib 估算（快很多）")
    parser.add_argument("--json", default="", help="把结果写到该 JSON 文件")
    args = parser.parse_args(argv)

    # 控制台是 GBK 时中文/特殊字符会炸，统一转 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from utils.app_paths import get_trace_dir

    trace_dir = Path(get_trace_dir())
    dbs = sorted(trace_dir.glob("agent_trace_??????.db"))
    if args.month:
        dbs = [p for p in dbs if args.month in p.name]
        if not dbs:
            print(f"未找到月库: {trace_dir}/*{args.month}*")
            return 1
    if not dbs:
        print(f"未找到月库: {trace_dir}")
        return 1

    compress = not args.no_compress
    results = []
    for db in dbs:
        info = probe_month(db, compress=compress)
        results.append(info)
        _print_month(info, compress=compress)

    total_file = sum(r["file_bytes"] for r in results)
    total_text = sum(r["text_bytes"] for r in results)
    print(f"\n=== 合计 {len(results)} 个月库 ===")
    print(f"文件 {_fmt(total_file)} · 文本 {_fmt(total_text)}（{total_text / total_file * 100:.1f}%）")
    steps_input = sum(r["columns"].get("steps.input", 0) for r in results)
    print(f"其中 steps.input 占文件 {steps_input / total_file * 100:.1f}%"
          f" —— 这是 T1 的主战场")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写出 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

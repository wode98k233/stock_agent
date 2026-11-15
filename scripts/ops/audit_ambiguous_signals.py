"""历史歧义信号审计：默认只读列出，--apply / --mark-ids 才写库。

用法：
  python scripts/ops/audit_ambiguous_signals.py                # 列 A/B 类
  python scripts/ops/audit_ambiguous_signals.py --apply        # 标记 A 类
  python scripts/ops/audit_ambiguous_signals.py --mark-ids 1190
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_eval.db import get_conn

_DIRECTION_WORDS = ("科技", "消费", "医疗", "半导体", "医药", "红利", "新能源", "军工")


def list_objectless(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """A 类：无对象且未归档大盘。"""
    return conn.execute(
        "SELECT * FROM signal WHERE symbol_key IS NULL AND is_market_wide=0 "
        "AND IFNULL(scope,'symbol')='symbol' ORDER BY signal_time DESC").fetchall()


def list_multi_direction(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """B 类：core_verdict 命中 ≥2 个方向词的存量信号（只列，供人工复核）。"""
    rows = conn.execute(
        "SELECT * FROM signal WHERE IFNULL(scope,'symbol')='symbol' "
        "AND core_verdict IS NOT NULL AND core_verdict <> '' ORDER BY signal_time DESC"
    ).fetchall()
    out = []
    for r in rows:
        hits = sum(1 for w in _DIRECTION_WORDS if w in (r["core_verdict"] or ""))
        if hits >= 2:
            out.append(r)
    return out


def apply_scope(conn: sqlite3.Connection, ids: list[int], scope: str = "legacy_ambiguous") -> int:
    """批量置 scope（保留行，只移出统计口径）。"""
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE signal SET scope=? WHERE id IN ({marks})", [scope, *ids])
    conn.commit()
    return cur.rowcount


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="标记 A 类（无对象）")
    ap.add_argument("--mark-ids", type=int, nargs="*", default=[], help="按 id 标记 B 类")
    args = ap.parse_args()

    conn = get_conn()
    a_rows = list_objectless(conn)
    print(f"[A] 无对象信号 {len(a_rows)} 条")
    for r in a_rows[:50]:
        print(f"  id={r['id']} time={r['signal_time']} decision={r['decision']} "
              f"verdict={(r['core_verdict'] or '')[:40]}")
    b_rows = list_multi_direction(conn)
    print(f"[B] 多方向存量信号（人工复核）{len(b_rows)} 条")
    for r in b_rows[:50]:
        print(f"  id={r['id']} symbol={r['symbol_key']} time={r['signal_time']} "
              f"verdict={(r['core_verdict'] or '')[:50]}")

    if args.apply:
        print("标记 A 类:", apply_scope(conn, [r["id"] for r in a_rows]))
    if args.mark_ids:
        print("标记指定 id:", apply_scope(conn, args.mark_ids))


if __name__ == "__main__":
    main()

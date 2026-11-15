"""补齐本地指数清单（index_info），供标的解析按名称查代码。

为什么：`index_info` 只有 25 条，常用行业指数（中证银行等）缺位，导致
「银行板块」这类名称只能落到板块代码或问句解析。这里用 akshare 的指数清单
一次性补齐（数据入库，代码里不写任何名称→代码）。

用法：
    python scripts/ops/backfill_index_info.py            # 预演
    python scripts/ops/backfill_index_info.py --apply    # 写入（幂等 upsert）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fetch() -> list[tuple[str, str]]:
    import akshare as ak
    df = ak.index_stock_info()
    out: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        code = str(row.get("index_code") or "").strip()
        name = str(row.get("display_name") or "").strip()
        if code and name:
            out.append((code, name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    from utils.app_paths import get_market_data_db_path
    path = args.db or get_market_data_db_path()
    rows = _fetch()
    print(f"akshare 指数清单: {len(rows)} 条；db: {path}")
    if not args.apply:
        for code, name in rows[:5]:
            print(f"  {code} {name}")
        print("（预演；加 --apply 才写入）")
        return 0

    conn = sqlite3.connect(path)
    try:
        from utils.db_schema.migrator import ensure_schema
        from utils.db_schema.tables import market_data as md
        ensure_schema(conn, md.TABLES, [])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        before = conn.execute("SELECT COUNT(*) FROM index_info").fetchone()[0]
        for code, name in rows:
            conn.execute(
                "INSERT INTO index_info (code, name, source, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (code, name, "akshare", now))
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM index_info").fetchone()[0]
        print(f"完成：index_info {before} → {after} 行")
        hit = conn.execute("SELECT code,name FROM index_info WHERE name LIKE '%银行%' LIMIT 5").fetchall()
        print("银行相关指数:", hit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

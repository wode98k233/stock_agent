"""把 data/symbol_aliases.json 灌进 market_data.db 的 symbol_alias 表（数据迁移，非代码）。

用法：
    python scripts/ops/seed_symbol_aliases.py            # 预演（只打印将要写入的行）
    python scripts/ops/seed_symbol_aliases.py --apply    # 实际写入（幂等 upsert）
    python scripts/ops/seed_symbol_aliases.py --apply --db /tmp/x.db   # 指定库（测试用）

写入实现只有一处：`backtest/symbol_alias.seed_from_data_file()`（查询层在缺库/空表时
也调它自动补种，保证「新环境忘了跑脚本」不会退化成「名称被当代码」）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEED = ROOT / "data" / "symbol_aliases.json"


def _db_path() -> str:
    from utils.app_paths import get_market_data_db_path
    return get_market_data_db_path()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只预演）")
    ap.add_argument("--db", default="", help="覆盖数据库路径（测试用）")
    args = ap.parse_args()

    data = json.loads(SEED.read_text(encoding="utf-8"))
    rows = data.get("aliases") or []
    if not rows:
        print("seed 为空，退出")
        return 1

    db = args.db or _db_path()
    print(f"seed: {len(rows)} 行；db: {db}")
    if not args.apply:
        for r in rows:
            print(f"  {r['alias']} -> {r['code']} ({r.get('kind') or 'etf'})")
        print("（预演；加 --apply 才写入）")
        return 0

    from backtest.symbol_alias import seed_from_data_file

    try:
        written = seed_from_data_file(db, source="seed")
    except Exception as e:  # noqa: BLE001 - 运维脚本：失败要打清楚，别抛栈吓人
        print(f"写入失败: {e}")
        return 1

    conn = sqlite3.connect(db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM symbol_alias").fetchone()[0]
    finally:
        conn.close()
    print(f"写入完成（{written} 行种子，幂等 upsert），symbol_alias 现有 {total} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

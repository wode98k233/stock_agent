#!/usr/bin/env python
"""记忆 chunk 元数据回填脚本（2026-09-23 修复）—— 幂等。

背景：``archive`` 一直把 ``chunk_index / chunk_count / chunk_group_id`` 写进
``MemoryEntry.metadata``，但 FTS5/Chroma 后端此前不持久化，导致 ``retrieve`` 的同报告
分组合并 + 文档序失效。新写入已落库；本脚本回填**存量**数据。

用法:
    python scripts/ops/backfill_memory_chunks.py [--dry-run] [--force] [--no-backup]

口径：按 ``(date[:10], source_query)`` 分组，组内按 rowid 升序赋 chunk_index；
      chunk_group_id = sha1(date|source_query)[:8]（稳定可重复）。
"""
import argparse
import os
import shutil
import sys
from datetime import datetime

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _backup_db(db_path: str) -> str | None:
    if not os.path.exists(db_path):
        return None
    dst = f"{db_path}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(db_path, dst)
    return dst


def main():
    ap = argparse.ArgumentParser(description="回填记忆 chunk 元数据（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--force", action="store_true", help="覆盖已有 chunk_group_id 的行")
    ap.add_argument("--no-backup", action="store_true", help="跳过 stock_memory.db 备份")
    args = ap.parse_args()

    from utils.app_paths import get_stock_memory_db_path
    db_path = get_stock_memory_db_path()

    if not args.dry_run and not args.no_backup:
        bak = _backup_db(db_path)
        print(f"[backup] {bak}" if bak else "[backup] 无库文件可备份")

    from memory.sdk import get_sdk
    sdk = get_sdk()

    if args.dry_run:
        from memory.backend.fts5 import FTS5Backend
        be = FTS5Backend(db_path)
        rows = be.get_all_raw(limit=10_000_000)
        groups: dict = {}
        for r in rows:
            groups.setdefault(f"{(r.get('date') or '')[:10]}|{r.get('source_query') or ''}", []).append(r)
        pending = sum(
            1 for items in groups.values() for r in items
            if args.force or not (r.get("chunk_group_id") or "")
        )
        print(f"[dry-run] checked={len(rows)} groups={len(groups)} pending={pending}")
        return

    res = sdk.backfill_chunk_metadata(force=args.force)
    print(f"[done] checked={res['checked']} updated={res['updated']}")


if __name__ == "__main__":
    main()

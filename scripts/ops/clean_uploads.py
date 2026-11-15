#!/usr/bin/env python
"""清理对话图片：孤儿 / 过期（默认 45 天）/ 超量（默认 500MB）。

默认只预演，加 --apply 才真正删除。日常由后台巡检 uploads_cleanup 自动执行。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="清理对话图片（默认预演）")
    p.add_argument("--days", type=int, default=None, help="保留天数（默认取 UPLOAD_RETENTION_DAYS）")
    p.add_argument("--max-total-mb", type=int, default=None, help="目录总量上限（默认取 UPLOAD_MAX_TOTAL_MB）")
    p.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    args = p.parse_args(argv)

    from server.uploads import cleanup

    res = cleanup(retention_days=args.days, max_total_mb=args.max_total_mb,
                  dry_run=not args.apply)
    mb = res["freed_bytes"] / 1048576
    print(f"[UPLOADS] {'预演' if res['dry_run'] else '已执行'}："
          f"待删/已删 {res['deleted']} 个 · 释放 {mb:.1f} MB · "
          f"保留 {res['kept']} 个（共 {res['total_bytes'] / 1048576:.1f} MB）")
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

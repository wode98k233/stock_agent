"""能力清单 / 扩展提议的运维入口（AI 出方案 → 人审）。

用法：
    venv/Scripts/python.exe scripts/ops/capability_proposals.py catalog
    venv/Scripts/python.exe scripts/ops/capability_proposals.py list [--status pending_review]
    venv/Scripts/python.exe scripts/ops/capability_proposals.py propose --name highest --kind indicator --detail "引擎没有"
    venv/Scripts/python.exe scripts/ops/capability_proposals.py propose --run SR-20261014-57282A
    venv/Scripts/python.exe scripts/ops/capability_proposals.py review --path <文件名> --decision approved --note "可以做"
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


def _gaps_of_run(run_uuid: str) -> list:
    from utils.cache.backtest_db import load_intent_checks
    checks = load_intent_checks(run_uuid) or {}
    out, seen = [], set()
    for _round, payload in checks.items():
        for gap in ((payload or {}).get('gaps') or []):
            key = (gap.get('kind'), gap.get('name'))
            if gap.get('name') and key not in seen:
                seen.add(key)
                out.append(gap)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="引擎能力清单与扩展提议")
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('catalog', help='打印引擎能力表')
    p_list = sub.add_parser('list', help='列出提议')
    p_list.add_argument('--status', default='')
    p_prop = sub.add_parser('propose', help='生成扩展方案（AI 出，待审）')
    p_prop.add_argument('--name', default='')
    p_prop.add_argument('--kind', default='indicator')
    p_prop.add_argument('--detail', default='')
    p_prop.add_argument('--run', default='', help='按某次研发的能力缺口批量出方案')
    p_rev = sub.add_parser('review', help='人审：approved / rejected')
    p_rev.add_argument('--path', required=True)
    p_rev.add_argument('--decision', required=True)
    p_rev.add_argument('--note', default='')
    args = ap.parse_args()

    from backtest import capability_proposals as cp

    if args.cmd == 'catalog':
        from backtest.json_compiler import export_capabilities
        print(json.dumps(export_capabilities(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == 'list':
        for item in cp.list_proposals(args.status or None):
            print('[%s] %s/%s — %s' % (item.get('status'), item.get('kind'), item.get('name'),
                                       str(item.get('summary') or '')[:60]))
            print('   ', Path(str(item.get('path'))).name)
        return 0
    if args.cmd == 'propose':
        gaps = _gaps_of_run(args.run) if args.run else [
            {'kind': args.kind, 'name': args.name, 'detail': args.detail}]
        if not gaps:
            print('这次研发没有记录到能力缺口（引擎都表达得了）')
            return 0
        for gap in gaps[:3]:
            res = cp.propose_extension(gap)
            if not res.get('ok'):
                print('生成失败：%s（%s）' % (res.get('reason'), gap.get('name')))
                continue
            path = cp.save_proposal(res['proposal'])
            print('已生成待审方案：%s' % path)
            print('  ', str(res['proposal'].get('summary') or '')[:80])
        return 0
    if args.cmd == 'review':
        res = cp.review_proposal(args.path, args.decision, args.note)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

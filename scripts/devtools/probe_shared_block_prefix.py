# -*- coding: utf-8 -*-
"""共享块前缀族探针统一入口。

合并自 `probe_classify_clarify_prefix` / `probe_pdor_planner_prefix`
（实现保留在 `_probes/`，逻辑零改动；pdor 复用 classify 的装配助手）。

用法::

    python scripts/devtools/probe_shared_block_prefix.py <子命令> [该子命令原有参数]

子命令（→ 原脚本）：
    classify  classify / clarify 的消息形态与共享前缀（record 基线 / check 断言不变性） （probe_classify_clarify_prefix.py）
    pdor      PDOR planner 的消息形态与共享前缀（同上 record / check）                    （probe_pdor_planner_prefix.py）

例：`python scripts/devtools/probe_shared_block_prefix.py classify --record out.json`
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent

_SUBS = {
    "classify": "probe_classify_clarify_prefix",
    "pdor": "probe_pdor_planner_prefix",
}

_DESC = {
    "classify": "classify / clarify 消息形态与共享前缀（record/check）",
    "pdor": "PDOR planner 消息形态与共享前缀（record/check）",
}


def _usage() -> str:
    lines = ["用法: python scripts/devtools/probe_shared_block_prefix.py <子命令> [参数]", "", "子命令："]
    lines += [f"  {sub:<10} {desc}" for sub, desc in _DESC.items()]
    return "\n".join(lines)


def main() -> int:
    for p in (_HERE, _ROOT):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0 if argv else 2
    sub, rest = argv[0], argv[1:]
    mod_name = _SUBS.get(sub)
    if mod_name is None:
        print(f"未知子命令: {sub}\n\n{_usage()}", file=sys.stderr)
        return 2
    sys.argv = [mod_name, *rest]
    mod = importlib.import_module(f"_probes.{mod_name}")
    rc = mod.main()
    return int(rc) if rc is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())

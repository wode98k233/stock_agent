# -*- coding: utf-8 -*-
"""报告链路 fork 族验证统一入口。

合并自 `verify_report_fork_shape` / `verify_report_fork_order`（实现保留在 `_probes/`，逻辑零改动）。

用法::

    python scripts/devtools/verify_report_fork.py <子命令> [该子命令原有参数]

子命令（→ 原脚本）：
    shape  报告三条调用（select/fast/expand）是否同属一个前缀族 + 5 个 builder 的 system 字节一致性 （verify_report_fork_shape.py）
    order  直接量 report-fast 命中率（≥50% 健康），异常时列出前一跳调用构成                    （verify_report_fork_order.py）

例：`python scripts/devtools/verify_report_fork.py order --months 202610`
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent

_SUBS = {
    "shape": "verify_report_fork_shape",
    "order": "verify_report_fork_order",
}

_DESC = {
    "shape": "三条调用是否同前缀族 + 5 个 builder 的 system 字节一致性",
    "order": "直接量 report-fast 命中率（≥50% 健康）",
}


def _usage() -> str:
    lines = ["用法: python scripts/devtools/verify_report_fork.py <子命令> [参数]", "", "子命令："]
    lines += [f"  {sub:<8} {desc}" for sub, desc in _DESC.items()]
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

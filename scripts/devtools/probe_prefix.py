# -*- coding: utf-8 -*-
"""前缀族探针统一入口。

合并自 `probe_report_cache` / `probe_cache_by_node` / `probe_fork_prefix_shape` /
`probe_prefix_layout` / `probe_prefix_monotonicity`（实现保留在 `_probes/`，逻辑零改动）。

用法::

    python scripts/devtools/probe_prefix.py <子命令> [该子命令原有参数]

子命令（→ 原脚本）：
    report-cache   provider 是否缓存「模板派生的静态 system 前缀」及 TTL   （probe_report_cache.py）
    cache-by-node  按 node/label 聚合真实调用的命中 / 成本拆分              （probe_cache_by_node.py）
    shape          主前缀分段 + 字节稳定性 + 副调用 fork 前后成本对照        （probe_fork_prefix_shape.py）
    layout         L0/L1/L2 三种消息布局的缓存命中对照（`--dry-run` 不联网） （probe_prefix_layout.py）
    monotonicity   逐节点 / 相邻调用的共同前缀与片段表（只读 trace）         （probe_prefix_monotonicity.py）

例：`python scripts/devtools/probe_prefix.py monotonicity --runs 5`
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent

# 子命令 → _probes 内实现模块名
_SUBS = {
    "report-cache": "probe_report_cache",
    "cache-by-node": "probe_cache_by_node",
    "shape": "probe_fork_prefix_shape",
    "layout": "probe_prefix_layout",
    "monotonicity": "probe_prefix_monotonicity",
}


def _usage() -> str:
    lines = ["用法: python scripts/devtools/probe_prefix.py <子命令> [参数]", "", "子命令："]
    lines += [f"  {sub:<14} {desc}" for sub, desc in _DESC.items()]
    return "\n".join(lines)


_DESC = {
    "report-cache": "provider 静态 system 前缀缓存与 TTL 探测",
    "cache-by-node": "按 node/label 聚合命中与成本拆分",
    "shape": "主前缀分段 / 字节稳定性 / fork 前后成本对照",
    "layout": "L0/L1/L2 消息布局命中对照（--dry-run 不联网）",
    "monotonicity": "逐节点前缀单调性与片段表（只读 trace）",
}


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

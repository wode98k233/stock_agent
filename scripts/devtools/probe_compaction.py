# -*- coding: utf-8 -*-
"""上下文压缩族探针统一入口。

合并自 `probe_compaction_cost` / `probe_compaction_impact` / `probe_requery_rate`
（实现保留在 `_probes/`，逻辑零改动）。

用法::

    python scripts/devtools/probe_compaction.py <子命令> [该子命令原有参数]

子命令（→ 原脚本）：
    cost     压缩链路的峰值 / 无压缩峰值 / 输入输出构成 / 收费情景 / 预算占用 （probe_compaction_cost.py）
    impact   按 run 统计压缩次数 / agent 命中率 / 观测峰值 / run 总量占预算比   （probe_compaction_impact.py）
    requery  压缩次数 × 完全重复工具调用率（按 tool_call_id 去重）              （probe_requery_rate.py）

例：`python scripts/devtools/probe_compaction.py cost --months 202610`
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent

_SUBS = {
    "cost": "probe_compaction_cost",
    "impact": "probe_compaction_impact",
    "requery": "probe_requery_rate",
}

_DESC = {
    "cost": "压缩峰值 / 无压缩峰值 / 输入输出构成 / 收费情景 / 预算占用",
    "impact": "按 run 统计压缩次数 / 命中率 / 峰值 / 预算占比",
    "requery": "压缩次数 × 完全重复工具调用率（按 tool_call_id 去重）",
}


def _usage() -> str:
    lines = ["用法: python scripts/devtools/probe_compaction.py <子命令> [参数]", "", "子命令："]
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

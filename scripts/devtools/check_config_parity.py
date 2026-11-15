"""config/ 拆包前后的属性一致性校验（PRD：config 拆包零改动护栏）。

用法:
    捕获基线:  python scripts/devtools/check_config_parity.py capture <output.json>
    对比:      python scripts/devtools/check_config_parity.py check <before.json> <after.json>

只比较「非可调用、非模块、非类」的标量类属性（repr 后比较），方法/内部辅助不参与。
"""
import importlib
import json
import os
import sys

# 直接执行脚本时 sys.path[0] 是 scripts/devtools，把仓库根插进去才能 import config
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _scalar_attrs(module_name: str) -> dict:
    mod = importlib.import_module(module_name)
    cfg = getattr(mod, "Config", None) or getattr(mod, "config")
    snap = {}
    # vars(cls) 只含本类 __dict__；属性可能来自 mixin 基类，必须遍历 MRO 汇总
    for klass in reversed(cfg.__mro__):
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if name.startswith("__"):
                continue
            if callable(value) or isinstance(value, (classmethod, staticmethod, type)):
                continue
            snap[name] = repr(value)
    return snap


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "capture":
        snap = _scalar_attrs("config")
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=0, sort_keys=True)
        print(f"captured {len(snap)} attrs -> {sys.argv[2]}")
    elif cmd == "check":
        with open(sys.argv[2], encoding="utf-8") as f:
            before = json.load(f)
        after = _scalar_attrs(sys.argv[3])
        missing = sorted(set(before) - set(after))
        added = sorted(set(after) - set(before))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        if missing or changed:
            print("FAIL")
            for k in missing:
                print(f"  MISSING: {k}")
            for k in changed:
                print(f"  CHANGED: {k}\n    before: {before[k][:60]}\n    after : {after[k][:60]}")
            for k in added:
                print(f"  ADDED(new, review): {k} = {after[k][:60]}")
            sys.exit(1)
        print(f"OK: {len(after)} attrs identical ({len(added)} new attrs need review)" if added
              else f"OK: {len(after)} attrs identical")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()

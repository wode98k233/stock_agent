"""工具 description 体检（只读，不改码）——PRD-A9 §2.3.2 迁移护栏

背景：tools 数组是缓存前缀的位置 0，一旦变化整条前缀作废。
description 越短，位置 0 的固定成本越低；而数据源回落、口径边界等业务口径
应移出 description，改由 usage_guide 在「激活技能时」按需投递。

约定（见 docs/superpowers/specs/2026-10-12-docstring-split-and-activate-skill-design.md）：
  docstring 第一段 → description（进 tools 数组）
  其余段落        → usage_guide（按需投递）

用法：
    python scripts/ops/check_tool_descriptions.py            # 报告
    python scripts/ops/check_tool_descriptions.py --strict   # 超阈值退出码 1（接 CI）
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 出现在 description 里通常意味着「业务口径没挪走」
CALIBER_KEYWORDS = ("回落", "兜底", "fallback", "不在本工具", "语义不同构",
                    "注：", "注意：", "主源", "已做体积约束")

# 超过这个长度才提示；60 是设计稿的软上限，但参数枚举类属正常放行（见下）
WARN_LEN = 60
STRICT_LEN = 120


def collect_tools():
    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from tools.skill_register import SkillRegister

    logger, _, _, _ = get_logger("check-tool-desc")
    reg = SkillRegister()
    reg.auto_discover()
    return reg.build_all_langchain_tools(logger, MemoryManager(logger))


def is_param_enum(desc: str) -> bool:
    """description 长，但内容是在枚举参数取值 —— 这是该留着的。

    设计稿 §1.3 反例 B：参数枚举必须紧邻 description，否则模型看不到
    「只能是这几个值」就会传错。把这类压到 60 字以内属于以准确率换 token。
    """
    signals = sum(desc.count(s) for s in ("/", "、", "(", "）", "可选", "默认", "，可选"))
    return signals >= 6 and "：" in desc[:80]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="存在 >120 字且非参数枚举的 description 时退出码 1")
    args = ap.parse_args()

    tools = collect_tools()
    rows = sorted(
        ((t.name, len(getattr(t, "description", "") or ""),
          getattr(t, "description", "") or "") for t in tools),
        key=lambda r: -r[1],
    )
    total = sum(r[1] for r in rows)
    n = len(rows)

    print("=" * 78)
    print(f"工具总数 {n}    description 总字符 {total:,}  (~{total/1.5:,.0f} token)")
    print("=" * 78)

    bad, warned, exempted = [], [], []
    for name, ln, desc in rows:
        if ln <= WARN_LEN:
            continue
        if is_param_enum(desc):
            exempted.append((name, ln, desc))
        elif ln > STRICT_LEN:
            bad.append((name, ln, desc))
        else:
            warned.append((name, ln, desc))

    print(f"\n=== 需要拆分（> {STRICT_LEN} 字且非参数枚举）: {len(bad)} ===")
    for name, ln, desc in bad:
        print(f"  {name:<32} {ln:>4} 字")
        print(f"      {desc[:88]}")

    print(f"\n=== 建议拆分（{WARN_LEN}~{STRICT_LEN} 字）: {len(warned)} ===")
    for name, ln, desc in warned:
        print(f"  {name:<32} {ln:>4} 字   {desc[:56]}")

    print(f"\n=== 豁免（参数枚举，保留）: {len(exempted)} ===")
    for name, ln, desc in exempted:
        print(f"  {name:<32} {ln:>4} 字   {desc[:44]}")

    print(f"\n=== 业务口径关键词混入 description ===")
    hits = 0
    for name, ln, desc in rows:
        matched = [k for k in CALIBER_KEYWORDS if k in desc]
        if matched:
            hits += 1
            print(f"  {name:<32} 命中 {matched}")
    if not hits:
        print("  （无）")

    cap = sum(min(r[1], STRICT_LEN) for r in rows)
    print(f"\n=== 总量 ===")
    print(f"  当前           {total:,} 字 (~{total/1.5:,.0f} token)")
    print(f"  全部压到 {STRICT_LEN} 字 {cap:,} 字 (~{cap/1.5:,.0f} token)")

    if bad:
        print(f"\n共 {len(bad)} 个待拆分。迁移方式：把 docstring 首段之后的内容"
              f"另起一段（空行分隔）即可，无需改写文本。")
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())

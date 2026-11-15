"""生成因子先验文件 ``output/factor_prior.json``（S4 F5-A）。

数据来源（**不自己跑回测**，只读已落库的评估结果）：
- ``backtest.factor_store.list_reports()``：每个池的最新一份因子评估报告
- ``output/factor_pool_compare_*.json``（最新）：跨池一致性（符号是否相反）

用法
----
    python scripts/ops/build_factor_prior.py            # 用已有报告汇总
    python scripts/ops/build_factor_prior.py --print    # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="生成因子先验")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="只打印摘要，不写 output/factor_prior.json")
    args = ap.parse_args()

    from backtest.factor_prior import build_prior, write_prior

    data = build_prior()
    src = data.get("source") or {}
    s = data.get("summary") or {}

    print(f"池：{src.get('pools')} | 报告 {src.get('reports')} 份 | "
          f"跨池数据：{'有' if src.get('cross_pool') else '无'}")
    print(f"因子总数 {s.get('n')}")
    print(f"  稳健（两池 DSR≥0.95 且同号）：{s.get('robust') or '无'}")
    print(f"  跨池变号：{s.get('fragile') or '无'}")
    print(f"  方向为负：{(s.get('reverse') or [])[:8]}{' …' if len(s.get('reverse') or []) > 8 else ''}")
    print()
    print("注意：" + str(s.get("caveat")))

    if args.print_only:
        print("\n（--print：未写文件）")
        return 0
    write_prior(data)
    print(f"\n已写入 output/factor_prior.json（{len(json.dumps(data))} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""按 node / label 统计真实 LLM 调用的缓存命中情况（只读 trace 月库）。

用法：
    python scripts/devtools/probe_cache_by_node.py [--months 202609,202610] [--runs 0]
        [--sequence RUN_ID] [--top 40]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRACE_DIR = ROOT / "db" / "agent_trace"


def _month_files(months: list[str]) -> list[Path]:
    if months:
        return [TRACE_DIR / f"agent_trace_{m}.db" for m in months]
    return sorted(TRACE_DIR.glob("agent_trace_*.db"))


def _llm_rows(path: Path):
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            "select id, run_id, step_name, started_at, extra from steps "
            "where step_type='llm' order by id"
        )
        for row in cur:
            try:
                extra = json.loads(row["extra"] or "{}")
            except Exception:
                continue
            if extra.get("cache_hit"):
                yield dict(run_id=row["run_id"], step_id=row["id"],
                           at=row["started_at"], label="-", node="(a8-cache-hit)",
                           inp=0, cached=0, out=0, cost=None, phase="a8")
                continue
            tu = extra.get("token_usage") or {}
            cost = extra.get("cost") or {}
            yield dict(
                run_id=row["run_id"],
                step_id=row["id"],
                at=row["started_at"],
                label=str(extra.get("label") or row["step_name"] or ""),
                # 归因优先取 purpose（细分用途），回落 node（PRD-A11 FR-7）
            node=str(extra.get("purpose") or extra.get("node") or ""),
                model=str(extra.get("model_name") or ""),
                inp=int(tu.get("input_tokens") or 0),
                cached=int(tu.get("cached_tokens") or 0),
                out=int(tu.get("output_tokens") or 0),
                reasoning=int(tu.get("reasoning_tokens") or 0),
                cost=cost.get("total"),
                c_miss=cost.get("input_miss"),
                c_hit=cost.get("input_hit"),
                c_out=cost.get("output"),
                c_saved=cost.get("cached_saved"),
                billing=str(cost.get("billing") or ""),
                provider=str(cost.get("provider") or ""),
            )
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--sequence", default="")
    ap.add_argument("--runs", type=int, default=0, help="列出最近 N 个 run 的调用序列")
    args = ap.parse_args()

    months = [m.strip() for m in args.months.split(",") if m.strip()]
    files = _month_files(months)
    rows: list[dict] = []
    for f in files:
        if f.exists():
            rows.extend(_llm_rows(f))

    if args.sequence:
        fixed = [r for r in rows if r["run_id"] == args.sequence]
        fixed.sort(key=lambda r: r["step_id"])
        print(f"run {args.sequence} 共 {len(fixed)} 次 LLM 调用")
        for i, r in enumerate(fixed, 1):
            ratio = (r["cached"] / r["inp"]) if r["inp"] else 0.0
            print(f"{i:3d} {r['at']} {r['node']:<22} {r['label'][:14]:<14} "
                  f"in={r['inp']:>7} cached={r['cached']:>7} hit={ratio:6.1%} out={r['out']:>5}")
        return 0

    print(f"扫描 {len(files)} 个月库，共 {len(rows)} 次真实 LLM 调用\n")

    def agg(key_fn):
        g = defaultdict(lambda: dict(n=0, inp=0, cached=0, out=0, cost=0.0, cold=0,
                                     c_miss=0.0, c_hit=0.0, c_out=0.0))
        for r in rows:
            k = key_fn(r)
            b = g[k]
            b["n"] += 1
            b["inp"] += r["inp"]
            b["cached"] += r["cached"]
            b["out"] += r["out"]
            if isinstance(r["cost"], (int, float)):
                b["cost"] += float(r["cost"])
            for src, dst in (("c_miss", "c_miss"), ("c_hit", "c_hit"), ("c_out", "c_out")):
                v = r.get(src)
                if isinstance(v, (int, float)):
                    b[dst] += float(v)
            if r["inp"] and r["cached"] / r["inp"] < 0.05:
                b["cold"] += 1
        return g

    def dump(title, g):
        print(f"== {title} ==")
        print(f"{'key':<30}{'n':>5}{'input':>10}{'cached':>10}{'hit':>7}{'cold%':>7}"
              f"{'out':>9}{'cost¥':>9}{'miss¥':>8}{'hit¥':>8}{'out¥':>8}")
        for k, b in sorted(g.items(), key=lambda kv: -kv[1]["c_miss"])[: args.top]:
            hit = b["cached"] / b["inp"] if b["inp"] else 0.0
            cold = b["cold"] / b["n"] if b["n"] else 0.0
            print(f"{k[:29]:<30}{b['n']:>5}{b['inp']:>10}{b['cached']:>10}"
                  f"{hit:>7.0%}{cold:>7.0%}{b['out']:>9}{b['cost']:>9.4f}"
                  f"{b['c_miss']:>8.3f}{b['c_hit']:>8.3f}{b['c_out']:>8.3f}")
        print()

    dump("按 node", agg(lambda r: r["node"] or "(none)"))
    dump("按 label", agg(lambda r: r["label"] or "(none)"))

    tot_in = sum(r["inp"] for r in rows)
    tot_ca = sum(r["cached"] for r in rows)
    print(f"总计 input={tot_in} cached={tot_ca} 命中率={tot_ca / tot_in:.1%}" if tot_in else "无数据")

    if args.runs:
        run_ids = []
        seen = set()
        for r in sorted(rows, key=lambda r: r["step_id"], reverse=True):
            if r["run_id"] not in seen:
                seen.add(r["run_id"])
                run_ids.append(r["run_id"])
            if len(run_ids) >= args.runs:
                break
        for rid in run_ids:
            fixed = sorted([r for r in rows if r["run_id"] == rid], key=lambda r: r["step_id"])
            print(f"\n-- run {rid} ({len(fixed)} calls) --")
            for i, r in enumerate(fixed, 1):
                ratio = (r["cached"] / r["inp"]) if r["inp"] else 0.0
                print(f"{i:3d} {r['node']:<20} {r['label'][:12]:<12} in={r['inp']:>7} "
                      f"cached={r['cached']:>7} hit={ratio:6.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

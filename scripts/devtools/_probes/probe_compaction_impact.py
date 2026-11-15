# -*- coding: utf-8 -*-
"""压缩代价探针：用实测 trace 量化上下文压缩对命中率、上下文峰值与成本的影响。

核心恒等式（可验证、不依赖重建消息）：

    无压缩峰值 ~= 观测到的 agent 峰值 + Σ(每次压缩被摘掉的 pending 文本 token)

依据：react-context-summary 步骤的 input_tokens 就是 format_rounds_for_summary(pending)
的体量（system 仅 263 tok），而这段 pending 在压缩后从对话里消失了。

用法：
    python scripts/devtools/probe_compaction_impact.py --months 202609,202610
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[3]
TRACE_DIR = ROOT / "db" / "agent_trace"
AGENT_NODES = {"agent"}


def _load(month: str):
    path = TRACE_DIR / f"agent_trace_{month}.db"
    if not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    runs: dict = {}
    for row in con.execute(
        "select id, run_id, step_name, extra from steps where step_type='llm' order by id"
    ):
        try:
            extra = json.loads(row["extra"] or "{}")
        except Exception:
            continue
        if extra.get("cache_hit"):
            continue
        tu = extra.get("token_usage") or {}
        co = extra.get("cost") or {}
        node = str(extra.get("purpose") or extra.get("node") or "")
        m = runs.setdefault(row["run_id"], {
            "agent_steps": [], "comps": [], "total_in": 0, "total_out": 0,
            "cost": 0.0, "nodes": defaultdict(lambda: [0, 0, 0]),
        })
        inp = int(tu.get("input_tokens") or 0)
        out = int(tu.get("output_tokens") or 0)
        cac = int(tu.get("cached_tokens") or 0)
        m["total_in"] += inp
        m["total_out"] += out
        if isinstance(co.get("total"), (int, float)):
            m["cost"] += float(co["total"])
        b = m["nodes"][node]
        b[0] += 1
        b[1] += inp
        b[2] += cac
        if node in AGENT_NODES:
            m["agent_steps"].append((inp, cac))
        if node == "react-context-summary" and inp > 0:
            m["comps"].append(inp)
    con.close()
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="202609,202610")
    ap.add_argument("--budget", type=int, default=500000)
    ap.add_argument("--top", type=int, default=16)
    args = ap.parse_args()

    runs: dict = {}
    for month in [m.strip() for m in args.months.split(",") if m.strip()]:
        runs.update(_load(month))

    rows = []
    for rid, m in runs.items():
        if not m["agent_steps"]:
            continue
        peak = max(s[0] for s in m["agent_steps"])
        comp_sum = sum(max(c - 300, 0) for c in m["comps"])
        raw_peak = peak + comp_sum
        ai = sum(s[0] for s in m["agent_steps"])
        ac = sum(s[1] for s in m["agent_steps"])
        rows.append(dict(rid=rid, n_agent=len(m["agent_steps"]), n_comp=len(m["comps"]),
                         ai=ai, ac=ac, peak=peak, raw_peak=raw_peak,
                         total=m["total_in"] + m["total_out"], cost=m["cost"],
                         nodes=m["nodes"]))

    rows.sort(key=lambda r: -r["total"])
    print(f"参与统计的 run = {len(rows)}（{args.months}）")
    print("")
    print("== Top run：agent 命中率 vs 压缩次数 vs 上下文峰值 ==")
    print(f"{'run':<14}{'agent步':>7}{'压缩':>5}{'agent命中%':>11}{'峰值tok':>9}"
          f"{'估算无压缩峰值':>15}{'run总量':>9}{'预算%':>7}{'cost':>8}")
    for r in rows[: args.top]:
        hr = r["ac"] / r["ai"] if r["ai"] else 0
        print(f"{r['rid'][:12]:<14}{r['n_agent']:>7}{r['n_comp']:>5}{hr:>11.1%}"
              f"{r['peak']:>9}{r['raw_peak']:>15}{r['total']:>9}"
              f"{r['total'] / args.budget:>7.1%}{r['cost']:>8.3f}")
    print("")

    print("== 按压缩次数分组：agent 命中率 ==")
    grp = defaultdict(lambda: dict(n=0, ai=0, ac=0, peak=0, raw=0, cost=0.0))
    for r in rows:
        k = min(r["n_comp"], 6)
        g = grp[k]
        g["n"] += 1
        g["ai"] += r["ai"]
        g["ac"] += r["ac"]
        g["peak"] = max(g["peak"], r["peak"])
        g["raw"] = max(g["raw"], r["raw_peak"])
        g["cost"] += r["cost"]
    print(f"{'压缩次数':<10}{'run数':>6}{'agent input':>13}{'agent命中%':>11}"
          f"{'观测峰值':>10}{'估算无压缩峰值':>16}{'cost':>9}")
    for k in sorted(grp):
        g = grp[k]
        hr = g["ac"] / g["ai"] if g["ai"] else 0
        label = f"{k}" if k < 6 else "6+"
        print(f"{label:<10}{g['n']:>6}{g['ai']:>13}{hr:>11.1%}{g['peak']:>10}"
              f"{g['raw']:>16}{g['cost']:>9.3f}")
    print("")

    print("== 上下文峰值分布（估算关闭/调高阈值后的峰值）==")
    for t in (16000, 32000, 64000, 128000, 256000):
        n = sum(1 for r in rows if r["raw_peak"] > t)
        print(f"  估算无压缩峰值 > {t:>7} tok 的 run：{n:>4} / {len(rows)}"
              f"  ({n / max(len(rows), 1):.0%})")
    print("")

    print("== 预算占用（口径 = 全 run 所有 LLM 的 input+output）==")
    for t in (0.25, 0.5, 0.75, 1.0):
        n = sum(1 for r in rows if r["total"] > t * args.budget)
        print(f"  run 总量 > {t:.0%} 预算({args.budget})：{n} / {len(rows)}")
    top = sorted(rows, key=lambda r: -r["total"])[:3]
    for r in top:
        print(f"  最大 run {r['rid'][:12]}: total={r['total']}")
        for node, b in sorted(r["nodes"].items(), key=lambda kv: -kv[1][1])[:6]:
            print(f"      {node:<24} n={b[0]:>3} in={b[1]:>8} hit={b[2] / b[1] if b[1] else 0:>6.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

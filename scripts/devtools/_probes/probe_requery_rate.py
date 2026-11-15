# -*- coding: utf-8 -*-
"""重查频次探针：压缩次数 vs 完全重复工具调用率（只读 trace，零 LLM 调用）。

回答 PRD-A11 的开放问题：``REACT_CONTEXT_RECENT_ROUNDS`` 该定 0 还是 2？
判据 = 压缩是否真的带来"同一参数重复查询"的返工。

**口径陷阱（务必先读，本脚本踩过两次）**：

1. ❌ "取最后一个 llm step 当全量" —— 错。step 240（最后一次 LLM）里
   ``带 tool_calls 的消息 = 0``，它是收尾总结，会漏掉所有工具调用。
2. ❌ "按 (step_id, seq) 逐条计数" —— 也错。每个 llm step 存的是**该次调用的完整
   payload（含全部历史）**：step 164 的 seq=0~8 与 step 149 的 seq=0~8 逐字节相同。
   按此计数会算出 **62.8%** 的荒谬重复率（把历史重放当成重复调用）。

正确口径：**去重键用 ``tool_call_id``**。历史 AI 消息被重复写入，但真正的工具执行
只带一次唯一 id（``call_00_pq`` / ``call_00_hv`` …）。按 ``tool_call_id`` 去重后
得到真实调用序列，签名 = ``(工具名, 归一化参数)``。
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


def _files(months: list[str]) -> list[Path]:
    if months:
        return [TRACE_DIR / f"agent_trace_{m}.db" for m in months]
    return sorted(TRACE_DIR.glob("agent_trace_*.db"))


def _tool_calls_of_run(con, run_id: str) -> list[tuple[str, str]]:
    """该 run 的全部真实工具调用（按 tool_call_id 去重，见模块 docstring 的口径说明）。"""
    out = []
    seen_ids: set[str] = set()
    for r in con.execute(
        "SELECT m.tool_calls FROM messages m JOIN steps s ON s.id=m.step_id "
        "WHERE s.run_id=? AND s.step_type='llm' "
        "AND m.tool_calls NOT IN ('','[]','null') ORDER BY m.id",
        (run_id,),
    ):
        try:
            tcs = json.loads(r["tool_calls"])
        except Exception:
            continue
        if isinstance(tcs, dict):
            tcs = [tcs]
        for tc in tcs or []:
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or tc.get("tool_call_id") or "")
            if cid:
                if cid in seen_ids:
                    continue          # 历史重放，不是新调用
                seen_ids.add(cid)
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = tc.get("name") or fn.get("name", "")
            args = tc.get("args") or fn.get("arguments") or {}
            out.append((name, json.dumps(args, ensure_ascii=False, sort_keys=True)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="202609,202610")
    args = ap.parse_args()
    files = _files([m.strip() for m in args.months.split(",") if m.strip()])

    per_run: dict[str, dict] = {}
    for p in files:
        if not p.exists():
            continue
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        # 压缩次数
        comp: dict[str, int] = {}
        for r in con.execute(
            "SELECT run_id, COUNT(*) c FROM steps WHERE step_type='llm' "
            "AND json_extract(extra,'$.node')='react-context-summary' GROUP BY run_id"):
            comp[r[0]] = r["c"]
        for r in con.execute("SELECT id FROM runs"):
            rid = r["id"]
            calls = _tool_calls_of_run(con, rid)
            if not calls:
                continue
            dup = len(calls) - len(set(calls))
            per_run[rid] = {"n": len(calls), "dup": dup,
                            "rate": dup / len(calls), "compactions": comp.get(rid, 0)}
        con.close()

    n = len(per_run)
    if not n:
        print("无数据")
        return 1
    print(f"扫描 {len(files)} 个月库，{n} 个有工具调用的 run")
    print("口径：按 tool_call_id 去重后的真实工具调用序列\n")

    buckets: dict[str, list] = {"0 次": [], "1 次": [], "2 次+": []}
    for v in per_run.values():
        k = v["compactions"]
        b = "0 次" if k == 0 else ("1 次" if k == 1 else "2 次+")
        buckets[b].append(v)

    print("== 完全重复调用率（同名 + 同参数 = 纯返工）==")
    print(f"{'压缩次数':<9}{'run 数':>8}{'调用/run':>10}{'重复率均值':>12}"
          f"{'中位':>9}{'P90':>9}{'零重复 run':>12}")
    allr = []
    for b in ("0 次", "1 次", "2 次+"):
        v = buckets[b]
        if not v:
            continue
        rates = sorted(x["rate"] for x in v)
        k = len(v)
        allr += rates
        zero = sum(1 for r in rates if r == 0)
        print(f"{b:<9}{k:>8}{sum(x['n'] for x in v)/k:>10.1f}{sum(rates)/k:>12.1%}"
              f"{rates[k//2]:>9.1%}{rates[int(k*0.9)]:>9.1%}{zero/k:>11.0%}%")
    rates = sorted(allr)
    print(f"{'全部':<9}{len(rates):>8}{'':>10}{sum(rates)/len(rates):>12.1%}"
          f"{rates[len(rates)//2]:>9.1%}{rates[int(len(rates)*0.9)]:>9.1%}")

    # 相关性：压缩次数 与 重复率 是否正相关
    xs = [(v["compactions"], v["rate"]) for v in per_run.values() if v["n"] >= 8]
    if len(xs) >= 10:
        n_ = len(xs)
        mx = sum(x for x, _ in xs) / n_
        my = sum(y for _, y in xs) / n_
        cov = sum((x - mx) * (y - my) for x, y in xs) / n_
        sx = (sum((x - mx) ** 2 for x, _ in xs) / n_) ** .5
        sy = (sum((y - my) ** 2 for _, y in xs) / n_) ** .5
        r = cov / (sx * sy) if sx and sy else 0
        print(f"\n压缩次数 × 重复率 相关系数 r = {r:+.3f}（n={n_}，已滤掉调用数<8 的短 run）")
        print("  r ≈ 0  → 压缩与返工无关系，recent_rounds 定 0/2 都无数据支撑，靠机制推理决定")
        print("  r > 0  → 压缩确实诱发返工，保留最近轮次（=2）是必要的")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
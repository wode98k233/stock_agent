# -*- coding: utf-8 -*-
"""上下文洞察的三个「实测数字」探针（**只读**，零 LLM 调用）。

为什么有这个脚本（2026-10-17 任务 13 复审）：92.3% / 66.1% / 4.24 倍 最初只写在
AGENTS.md 与 CODE_WIKI.md §11.18 里、**没有第二来源** —— 那就是「不可独立复算的断言」。
本脚本从真实月库按下面钉死的口径把它们算出来。数字会随库内容增长而变，**引用前先跑一遍**。

口径（逐条钉死，避免"同一个 66.1% 有十种算法"）：

  ① **思考占输出** = Σ reasoning_tokens / Σ output_tokens，范围 = 该月库里**真实 llm 步**
     （step_type == "llm" 且 extra 未标 cache_hit 伪步骤）。
     2026-10 月库实测：1,109,086 / 1,677,231 = 66.13%（823 次调用，其中 768 次 reasoning > 0）。
  ② **ΣLLM/墙钟（逐 run 取中位）** = median(Σ llm 步 duration_ms / runs.duration_ms)，
     范围 = 该月里**墙钟 > 0 且有 llm 步**的 run。
     ⚠️ 立项时记的是 **92.3%**，但当时的筛选条件未记录：2026-10 今日复算 **93.5%**（58 run）；
     若把分母换成 Σ(llm+tool+collector) 则为 **91.9%** —— **以本脚本输出为准**。
  ③ **平铺求和 / 墙钟** = Σ**全部**步骤 duration_ms / runs.duration_ms，逐 run 算后取最大的几个。
     立项举例的 253 步 run（2026-10-01，id 13e9842c…）实测 **4.228 ≈ 4.24 倍** ——
     注意它是**某一个 run 的极值**，不是全库均值（全库最大可达 127 倍）。

只读语义：全部读经 utils.agent_trace.store 门面（与其他 probe_* 脚本同款），只跑 SELECT；不建表、
不写任何业务数据。注意门面自身按仓库既定语义开连接（WAL / 线程局部缓存），故 SQLite 侧的
journal 收尾等内部动作与其它读取路径一致，本脚本不做任何额外写操作。

用法：
    python scripts/devtools/context_insight_probe.py                 # 最新月库
    python scripts/devtools/context_insight_probe.py --month 202610  # 指定月（可重复）
    python scripts/devtools/context_insight_probe.py --all           # 所有存在的月库
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import statistics
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _facade():
    """延迟导入：store 门面 + 统计底座的公开入口（本脚本不碰任何私有实现细节）。"""
    sys.path.insert(0, str(ROOT))
    from utils.agent_trace import context_stats as cs
    from utils.agent_trace.store import store
    from utils.token_usage import normalize_token_usage_dict
    return cs, store, normalize_token_usage_dict


def collect(month: str, top_n: int = 3, progress: bool = True) -> dict:
    """算一个月的三个数字（只读）。month 形如 202610（月库键 = UTC 月）。"""
    cs, store, normalize = _facade()
    prefix = f"{month[:4]}-{month[4:]}"
    runs = [r for r in store.list_runs(limit=1000000)
            if str(r.get("created_at") or "").startswith(prefix)]
    calls = calls_pos = 0
    sum_reason = sum_out = 0
    ratio_wall: list = []
    ratio_three: list = []
    flat_rows: list = []
    runs_with_llm = 0
    for i, run in enumerate(runs, 1):
        if progress and i % 200 == 0:
            print(f"  ... {month} 已扫 {i}/{len(runs)} run", file=sys.stderr, flush=True)
        steps = store.get_steps_with_messages(run["id"])
        llm = [s for s in steps
               if s.get("step_type") == "llm"
               and not cs.parse_extra(s.get("extra")).get("cache_hit")]
        if not llm:
            continue
        runs_with_llm += 1
        for step in llm:
            extra = cs.parse_extra(step.get("extra"))
            tu = extra.get("token_usage")
            usage = normalize(tu if isinstance(tu, dict) else None)
            calls += 1
            sum_out += int(usage.get("output_tokens") or 0)
            reasoning = int(usage.get("reasoning_tokens") or 0)
            sum_reason += reasoning
            if reasoning > 0:
                calls_pos += 1
        timing = cs.build_timing(steps, run.get("duration_ms"))
        wall = float(timing["wall_ms"] or 0)
        if wall > 0:
            ratio_wall.append(timing["llm_ms"] / wall)
            three = timing["llm_ms"] + timing["tool_ms"] + timing["collector_ms"]
            if three > 0:
                ratio_three.append(timing["llm_ms"] / three)
            flat = sum(float(v or 0) for v in (timing["by_type"] or {}).values())
            flat_rows.append((flat / wall, len(steps), run["id"], run.get("created_at")))
    flat_rows.sort(reverse=True)                                   # 按比值（极值口径）
    by_steps = sorted(flat_rows, key=lambda x: x[1], reverse=True)  # 按步数（立项举例的那个 run）
    def _row(item):
        r, n, rid, ca = item
        return {"ratio": round(r, 3), "steps": n, "run_id": rid, "created_at": ca}
    return {
        "month": month,
        "runs_total": len(runs),
        "runs_with_llm": runs_with_llm,
        "llm_calls": calls,
        "calls_with_reasoning": calls_pos,
        "sum_reasoning_tokens": sum_reason,
        "sum_output_tokens": sum_out,
        "thinking_share_of_output": round(sum_reason / sum_out, 4) if sum_out else None,
        "median_sum_llm_over_wall": round(statistics.median(ratio_wall), 4) if ratio_wall else None,
        "mean_sum_llm_over_wall": round(statistics.mean(ratio_wall), 4) if ratio_wall else None,
        "median_llm_over_three": round(statistics.median(ratio_three), 4) if ratio_three else None,
        "n_runs_for_wall_ratio": len(ratio_wall),
        "max_flat_over_wall": round(flat_rows[0][0], 3) if flat_rows else None,
        "flat_over_wall_top": [_row(x) for x in flat_rows[:top_n]],
        # 立项举例的 4.24 倍是「253 步那个 run」——按步数取最大的那个，便于直接对上
        "largest_run_by_steps": _row(by_steps[0]) if by_steps else None,
    }


def render(res: dict) -> str:
    lines = [f"-- 月库 {res['month']} ------------------------------------------"]
    if not res["runs_total"]:
        lines.append("  （该月无 run）")
        return "\n".join(lines)
    lines += [
        f"  run 总数 {res['runs_total']}；其中有 llm 步 {res['runs_with_llm']}；"
        f"参与 ΣLLM/墙钟 统计 {res['n_runs_for_wall_ratio']}",
        f"  (1) 思考占输出 = {res['sum_reasoning_tokens']} / {res['sum_output_tokens']}"
        f" = {res['thinking_share_of_output']}"
        f"（{res['llm_calls']} 次调用，{res['calls_with_reasoning']} 次 reasoning > 0）",
        f"  (2) ΣLLM/墙钟 逐 run 中位 = {res['median_sum_llm_over_wall']}"
        f"（均值 {res['mean_sum_llm_over_wall']}；"
        f"若分母改 Σ(llm+tool+collector) 中位 = {res['median_llm_over_three']}）",
        f"  (3) 平铺求和/墙钟 最大 = {res['max_flat_over_wall']}",
    ]
    for row in res["flat_over_wall_top"]:
        lines.append(f"      {row['ratio']}x · {row['steps']} 步 · {row['run_id']} · {row['created_at']}")
    big = res.get("largest_run_by_steps")
    if big:
        lines.append(f"  (3b) 步数最多的 run（立项举例的那个）= {big['steps']} 步 · "
                     f"{big['ratio']}x · {big['run_id']} · {big['created_at']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="上下文洞察三个实测数字的只读复算探针")
    ap.add_argument("--month", action="append", default=None,
                    help="月库键，如 202610（可重复；默认=最新存在的月库）")
    ap.add_argument("--all", action="store_true", help="所有存在的月库")
    ap.add_argument("--top", type=int, default=3, help="(3) 平铺/墙钟 Top N（默认 3）")
    ap.add_argument("--json", action="store_true", help="额外打印 JSON")
    args = ap.parse_args(argv)

    _, store, _ = _facade()
    months = store.iter_existing_months()          # 新 -> 旧
    if args.all:
        picked = list(months)
    elif args.month:
        picked = list(args.month)
        missing = [m for m in picked if m not in months]
        if missing:
            print(f"⚠️ 月库不存在：{missing}（存在的是 {months}）", file=sys.stderr)
    else:
        picked = list(months[:1])
    if not picked:
        print("没有月库可算（db/agent_trace/agent_trace_YYYYMM.db）", file=sys.stderr)
        return 1

    results = []
    for month in picked:
        res = collect(month, top_n=args.top)
        results.append(res)
        print(render(res))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    print("\n⚠️ 引用 AGENTS.md / CODE_WIKI §11.18 的三个数字时以本输出为准："
          "(2) 立项时的 92.3% 其筛选条件未记录，同库今日复算见上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

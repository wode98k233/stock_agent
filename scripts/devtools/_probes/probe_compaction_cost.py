# -*- coding: utf-8 -*-
"""上下文压缩链路全景探针：峰值 / 无压缩峰值 / 成本情景 / 预算占用（只读 trace，不调 LLM）。

回答四个问题（PRD-A10 §2 的续）：
  1. 一次 run 的上下文峰值是多少？「把阈值调到 20 轮」会不会撑爆？
  2. 压缩调用的输入/输出各多少？**输出里的 reasoning 占多少**（压缩花钱的大头在输出侧）
  3. 「免费端点将来收费」情景下，压缩每次要花多少钱？走 fork 能省多少？
  4. 免费调用按 raw token 计入预算，吃掉了 MAX_TOKENS_PER_QUERY 的多少？

用法：
    python scripts/devtools/probe_compaction_cost.py                 # 扫全部月库
    python scripts/devtools/probe_compaction_cost.py --months 202610
    python scripts/devtools/probe_compaction_cost.py --sequence RUN_ID
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

# fork 后压缩调用的输入命中率：上一次 agent 调用刚发过同一条前缀，
# 只有「本次新增的工具结果 + 尾部指令」是冷的。用 0.8 做保守估计（实测每轮增长 3~6K）。
FORK_INPUT_HIT = 0.80

# 价目（data/pricing/pricing_catalog.default.json，deepseek-v4.1-flash，元/M）：
#   峰时 = 周一至周五 09:00-12:00 与 14:00-18:00（Asia/Shanghai），价格 ×2。
# 压缩链路现在走 sensenova（免费，valid_until=2026-12-31）；一旦收费就按下面两档计价。
RATE_OFFPEAK = {"in_miss": 1.0, "in_hit": 0.02, "out": 4.0}
RATE_PEAK = {"in_miss": 2.0, "in_hit": 0.04, "out": 8.0}

COMPACT_NODES = ("react-context-summary",)
TOOLCOMP_NODES = ("react-tool-compress",)


def _month_files(months: list[str]) -> list[Path]:
    if months:
        return [TRACE_DIR / f"agent_trace_{m}.db" for m in months]
    return sorted(TRACE_DIR.glob("agent_trace_*.db"))


def _rows(path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    runs = {r["id"]: dict(r) for r in con.execute("select * from runs")}
    out: list[dict] = []
    for r in con.execute("select id, run_id, started_at, extra from steps "
                         "where step_type='llm' order by id"):
        try:
            extra = json.loads(r["extra"] or "{}")
        except Exception:
            continue
        if extra.get("cache_hit"):
            continue
        tu = extra.get("token_usage") or {}
        cost = extra.get("cost") or {}
        run = runs.get(r["run_id"]) or {}
        out.append(dict(
            step=r["id"], run=r["run_id"], at=r["started_at"],
            mode=run.get("agent_name", "?"),
            # 归因优先取 purpose（细分用途），回落 node（PRD-A11 FR-7）
            node=str(extra.get("purpose") or extra.get("node") or ""),
            inp=int(tu.get("input_tokens") or 0),
            ca=int(tu.get("cached_tokens") or 0),
            out=int(tu.get("output_tokens") or 0),
            reason=int(tu.get("reasoning_tokens") or 0),
            billing=str(cost.get("billing") or "none"),
            prov=str(cost.get("provider") or ""),
            miss=float(cost.get("input_miss") or 0),
            hitc=float(cost.get("input_hit") or 0),
            outc=float(cost.get("output") or 0),
        ))
    con.close()
    return out


def _effective_rates(rows: list[dict]) -> tuple[float, float, float]:
    """用真实付费行反推单价（元/token，含峰谷），避免拿挂牌价当实测价。"""
    paid = [r for r in rows if r["billing"] == "paid"]
    miss_tok = sum(r["inp"] - r["ca"] for r in paid)
    hit_tok = sum(r["ca"] for r in paid)
    out_tok = sum(r["out"] for r in paid)
    return (sum(r["miss"] for r in paid) / miss_tok if miss_tok else 0.0,
            sum(r["hitc"] for r in paid) / hit_tok if hit_tok else 0.0,
            sum(r["outc"] for r in paid) / out_tok if out_tok else 0.0)


def _comp_cost(comp: list[dict], rate: dict) -> tuple[float, float]:
    """返回 (收费·不 fork, 收费·fork) 两次调用的成本。"""
    no_fork = sum(r["inp"] * rate["in_miss"] / 1e6 + r["out"] * rate["out"] / 1e6
                  for r in comp)
    forked = sum(r["inp"] * (1 - FORK_INPUT_HIT) * rate["in_miss"] / 1e6
                 + r["inp"] * FORK_INPUT_HIT * rate["in_hit"] / 1e6
                 + r["out"] * rate["out"] / 1e6 for r in comp)
    return no_fork, forked


def _run_view(rows: list[dict]) -> dict:
    agent = sorted([r for r in rows if r["node"] == "agent"], key=lambda x: x["step"])
    comp = sorted([r for r in rows if r["node"] in COMPACT_NODES], key=lambda x: x["step"])
    tcomp = [r for r in rows if r["node"] in TOOLCOMP_NODES and r["inp"] > 0]

    peak = max((r["inp"] for r in agent), default=0)
    # 无压缩峰值估算 = 观测峰值 + Σ(被摘掉的 pending 文本 − 摘要正文)。
    # 摘要正文用 content≈out−reason 的实测值代替（reasoning 不是可复用的信息）。
    comp_content = sum(max(0, r["out"] - r["reason"]) for r in comp)
    gain = sum(max(0, r["inp"]) for r in comp) - (comp_content if comp else 0)
    paid = [r for r in rows if r["billing"] == "paid"]
    free = [r for r in rows if r["billing"] == "free"]
    # 计价开关是后开的，早期行没有 cost 字段；但「谁走免费端点」由代码写死——
    # 只有 ReactContextBuilder（上下文压缩）与工具输出压缩读 COMPRESS_LLM_*，
    # 所以按 node 判定免费池，比按 billing 字段更早、更准。
    free_pool = [r for r in rows if r["node"] in COMPACT_NODES + TOOLCOMP_NODES]
    main_pool = [r for r in rows if r["node"] not in COMPACT_NODES + TOOLCOMP_NODES]
    return dict(
        run=rows[0]["run"], mode=rows[0]["mode"], at=rows[0]["at"],
        agent_calls=len(agent), peak=peak, no_comp_peak=peak + max(0, gain),
        comp_calls=len(comp), comp_in=sum(r["inp"] for r in comp),
        comp_out=sum(r["out"] for r in comp), comp_reason=sum(r["reason"] for r in comp),
        comp_content=comp_content,
        paid_cost=sum(r["miss"] + r["hitc"] + r["outc"] for r in paid),
        main_tok=sum(r["inp"] + r["out"] for r in main_pool),
        free_tok=sum(r["inp"] + r["out"] for r in free_pool),
        tcomp_in=sum(r["inp"] for r in tcomp), tcomp_out=sum(r["out"] for r in tcomp),
        comp_rows=comp,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="")
    ap.add_argument("--sequence", default="")
    args = ap.parse_args()

    files = _month_files([m.strip() for m in args.months.split(",") if m.strip()])
    rows: list[dict] = []
    for f in files:
        if f.exists():
            rows.extend(_rows(f))
    if not rows:
        print("无 LLM 调用数据")
        return 1

    m_r, h_r, o_r = _effective_rates(rows)
    print(f"扫描 {len(files)} 个月库 / {len(rows)} 次 LLM 调用")
    print(f"付费行反推实测单价（含峰谷）：miss={m_r*1e6:.3f}  hit={h_r*1e6:.4f}  "
          f"out={o_r*1e6:.3f} 元/M")
    print("（价目：闲时 1/0.02/4，峰时 2/0.04/8 元/M；峰=工作日 09-12 & 14-18 北京时间）\n")

    if args.sequence:
        sel = sorted([r for r in rows if r["run"] == args.sequence], key=lambda x: x["step"])
        print(f"run {args.sequence} 共 {len(sel)} 次调用\n")
        for i, r in enumerate(sel, 1):
            hit = r["ca"] / r["inp"] if r["inp"] else 0.0
            mark = "  ★上下文压缩" if r["node"] in COMPACT_NODES else ""
            print(f"{i:3d} {r['at'][:19]} {r['node']:<24} in={r['inp']:>6} ca={r['ca']:>6} "
                  f"hit={hit:>6.1%} out={r['out']:>6} reason={r['reason']:>6} "
                  f"bill={r['billing']}{mark}")
        return 0

    byrun: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byrun[r["run"]].append(r)
    views = [_run_view(v) for v in byrun.values() if any(x["node"] == "agent" for x in v)]
    n = len(views)
    priced = [v for v in views if v["paid_cost"] > 0]

    print(f"有效 run（≥1 次 agent 调用）= {n}，发生过上下文压缩 = "
          f"{sum(1 for v in views if v['comp_calls'])}，有计价记录 = {len(priced)}")

    print(f"\n== 上下文峰值（top 12 by 无压缩峰值估算）==")
    print(f"{'run':<9}{'mode':<14}{'agent次':>7}{'观测峰值':>9}{'压缩次':>7}"
          f"{'压缩省掉':>10}{'无压缩峰值':>11}{'单次压缩in':>11}{'单次压缩out':>12}")
    for v in sorted(views, key=lambda x: -x["no_comp_peak"])[:12]:
        c = max(1, v["comp_calls"])
        print(f"{v['run'][:8]:<9}{v['mode'][:13]:<14}{v['agent_calls']:>7}{v['peak']:>9}"
              f"{v['comp_calls']:>7}{max(0, v['no_comp_peak']-v['peak']):>10}"
              f"{v['no_comp_peak']:>11}{v['comp_in']//c:>11}{v['comp_out']//c:>12}")
    peaks = [v["no_comp_peak"] for v in views]
    for lim in (32_000, 48_000, 64_000, 96_000, 128_000):
        k = sum(1 for p in peaks if p > lim)
        print(f"  无压缩峰值 > {lim:>7,} tok 的 run：{k:>3}/{n} ({k/n:.0%})")
    print(f"  观测峰值最大 = {max(v['peak'] for v in views):,} tok；"
          f"无压缩峰值最大 = {max(peaks):,} tok（估算口径见脚本头注释）")

    allc = [r for r in rows if r["node"] in COMPACT_NODES and r["out"] > 0]
    print(f"\n== 压缩调用输出构成（花钱的大头在输出侧）==")
    if allc:
        tot_out = sum(r["out"] for r in allc)
        tot_rz = sum(r["reason"] for r in allc)
        k = len(allc)
        print(f"  n={k}  输出合计 {tot_out:,} tok  其中 reasoning {tot_rz:,} "
              f"({tot_rz/tot_out:.0%})、正文 {tot_out-tot_rz:,}")
        print(f"  单次均值：输入 {sum(r['inp'] for r in allc)//k:,} / 输出 {tot_out//k:,} / "
              f"正文 {(tot_out-tot_rz)//k:,}")
        print(f"  提示词里的 max_chars 只约束正文（≈{(tot_out-tot_rz)//k*3:,} 字符 < 6,500），"
              f"**约束不到 reasoning**")

    print(f"\n== 成本情景：sensenova 免费到期后的压缩链路（每 run 均值，n={n}）==")
    print(f"{'情景':<32}{'闲时 ¥':>10}{'峰时 ¥':>10}")
    comp_all = [r for v in views for r in v["comp_rows"]]
    off_nf, off_fk = _comp_cost(comp_all, RATE_OFFPEAK)
    pk_nf, pk_fk = _comp_cost(comp_all, RATE_PEAK)
    print(f"{'现状（sensenova 免费，至 2026-12-31）':<32}{0.0:>10.4f}{0.0:>10.4f}")
    print(f"{'收费 · 不 fork（现状形态）':<32}{off_nf/n:>10.4f}{pk_nf/n:>10.4f}")
    print(f"{'收费 · 走 fork（主前缀命中）':<32}{off_fk/n:>10.4f}{pk_fk/n:>10.4f}")
    print(f"\n  fork 相对「收费·不fork」省 ¥{(off_nf-off_fk)/n:.4f}/run（闲时）/ "
          f"¥{(pk_nf-pk_fk)/n:.4f}/run（峰时），全部来自输入侧")
    per_call_out = (sum(r["out"] for r in comp_all) / max(1, len(comp_all))
                    * RATE_OFFPEAK["out"] / 1e6)
    print(f"  输出侧每次固定 ¥{per_call_out:.4f}（闲时）——**压缩成本的主体，fork 省不掉**")
    print(f"  reasoning 占输出 {(sum(r['reason'] for r in comp_all)/max(1,sum(r['out'] for r in comp_all))):.0%}"
          f" → 想真降本，第一刀是关掉压缩调用的 reasoning，不是 fork")
    tc_in = sum(v["tcomp_in"] for v in views) / n
    tc_out = sum(v["tcomp_out"] for v in views) / n
    print(f"  （工具输出压缩本期不改，但一旦同样收费：in/run={tc_in:,.0f} out/run={tc_out:,.0f}"
          f" → 闲时 ¥{(tc_in*RATE_OFFPEAK['in_miss']+tc_out*RATE_OFFPEAK['out'])/1e6:.4f}/run）")

    print(f"\n== 预算占用（MAX_TOKENS_PER_QUERY 按 raw 记，命中/免费都不打折）==")
    raw = sum(v["main_tok"] + v["free_tok"] for v in views) / n
    free_tok = sum(v["free_tok"] for v in views) / n
    print(f"  每 run raw token = {raw:,.0f}（预算 {raw/500_000:.0%}），"
          f"其中免费端点 {free_tok:,.0f}（{free_tok/max(1,raw):.0%}）")
    print(f"  → 免费调用白占预算 {free_tok/max(1,raw):.0%}；加权口径（free≈0）后 ≈ "
          f"{raw-free_tok:,.0f}（{max(0.0,(raw-free_tok)/500_000):.0%}）")
    over = sum(1 for v in views if v["main_tok"] + v["free_tok"] > 500_000)
    print(f"  raw > 500,000 的 run：{over}/{n}")
    if priced:
        pc = sum(v["paid_cost"] for v in priced) / len(priced)
        print(f"\n== 付费链路实测（仅 {len(priced)} 个有计价记录的 run，n={len(priced)}）==")
        print(f"  每 run 实付 ¥{pc:.4f}；本期压缩链路贡献 ¥0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
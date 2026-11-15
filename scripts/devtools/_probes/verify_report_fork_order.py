# -*- coding: utf-8 -*-
"""报告链路健康度诊断（PRD-A11 C3）。

**为什么不是"顺序守卫"了**：最初的设计按「agent 与 report-fast 之间不得有非 fork 调用」
判违规，依据是 PRD-A11 §2.12 的"异族调用驱逐缓存"假说。**2026-10-16 的实测推翻了它**：

    两个真实 run（react_stock / unified_plan）里，`sentiment-extract`
    （in≈700~950 tok、无 fork 标记）都夹在「最后一次 agent 调用」与 `report-fast` 之间，
    而 report-fast 依然命中 **94% / 94%**、dashboard **72% / 70%**。

所以「中间有非 fork 调用」**不等于**会驱逐。继续按名字/标记判违规只会产生误报。

**改成看结果**：直接量 `report-fast` 的命中率，并把它前一跳的调用构成作为上下文打出来。
- 命中 ≥50% → 健康（PRD-A11 AC-3 的口径）
- 命中 <50% 且该 run 有 fork 埋点 → 报警，并列出区间内每次调用及其体量，供人工定位
- 无 fork 埋点（埋点上线前的历史数据）→ 只做统计，不判定

这样判据是**可验证的事实**（命中率就是缓存系统的输出），而不是一个我无法证实的机制假说。

用法：
    python scripts/devtools/verify_report_fork_order.py --months 202610
    python scripts/devtools/verify_report_fork_order.py --run <RUN_ID>
    python scripts/devtools/verify_report_fork_order.py --selftest
    python scripts/devtools/verify_report_fork_order.py --json
退出码：0 = 无告警；1 = 有 report-fast 命中异常。
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

AGENT_PURPOSES = {"agent", "react-sub"}
BODY_PURPOSE = "report-fast"
# AC-3 的口径
HEALTHY_HIT = 0.5


def _files(months: list[str]) -> list[Path]:
    if months:
        return [TRACE_DIR / f"agent_trace_{m}.db" for m in months]
    return sorted(TRACE_DIR.glob("agent_trace_*.db"))


def _calls(con, run_id: str) -> list[dict]:
    out = []
    for r in con.execute(
        "SELECT extra FROM steps WHERE run_id=? AND step_type='llm' ORDER BY id",
        (run_id,),
    ):
        try:
            e = json.loads(r["extra"] or "{}")
        except Exception:
            continue
        if e.get("cache_hit"):
            continue
        purpose = str(e.get("purpose") or e.get("node") or "")
        if not purpose:
            continue
        tu = e.get("token_usage") or {}
        out.append({
            "purpose": purpose,
            "fork_kind": str(e.get("fork_kind") or ""),
            "inp": int(tu.get("input_tokens") or 0),
            "ca": int(tu.get("cached_tokens") or 0),
            "hit": (int(tu.get("cached_tokens") or 0) / int(tu.get("input_tokens") or 1)
                    if tu.get("input_tokens") else 0.0),
        })
    return out


def diagnose(calls: list[dict]) -> dict:
    """返回 {verdict: healthy|alert|no_marker|skip, span, body, ...}。"""
    idx_agent = [i for i, c in enumerate(calls) if c["purpose"] in AGENT_PURPOSES]
    idx_body = [i for i, c in enumerate(calls) if c["purpose"] == BODY_PURPOSE]
    if not idx_agent or not idx_body:
        return {"verdict": "skip", "why": "no_agent_or_no_body"}
    start, end = idx_agent[-1], idx_body[-1]
    if end <= start:
        return {"verdict": "skip", "why": "body_before_last_agent"}
    span = calls[start + 1:end]
    body = calls[end]

    # 判据 = 结果（命中率），不是机制（谁夹在中间）
    has_marker = any(c["fork_kind"] for c in calls)
    if body["hit"] >= HEALTHY_HIT:
        return {"verdict": "healthy", "span": span, "body": body}
    return {"verdict": "alert" if has_marker else "no_marker",
            "span": span, "body": body}


def _selftest() -> int:
    """用构造数据锁住判据（无埋点数据时也跑得起来）。"""
    def C(purpose, inp, ca, fork=""):
        return {"purpose": purpose, "fork_kind": fork, "inp": inp, "ca": ca,
                "hit": ca / inp if inp else 0.0}

    cases = [
        ("健康：夹了 1K 非 fork 调用，report-fast 仍 94%（=2026-10-16 实测形态）",
         [C("agent", 25058, 18944), C("template_report", 950, 0),
          C("report-fast", 26945, 25328, "llm_fork"), C("dashboard", 32064, 24960, "llm_fork")],
         "healthy"),
        ("告警：report-fast 命中 0% 且有埋点 → 真出事了",
         [C("agent", 20000, 15000), C("template_report", 1000, 0),
          C("report-fast", 21000, 0, "llm_fork")],
         "alert"),
        ("无埋点：历史数据（命中 0%）→ 不判定，只统计",
         [C("agent", 20000, 0), C("template_report", 1000, 0), C("report-fast", 21000, 0)],
         "no_marker"),
        ("健康：agent 与 report-fast 直接相连",
         [C("agent", 20000, 19000), C("report-fast", 21000, 20000, "llm_fork")],
         "healthy"),
        ("跳过：没有 report-fast",
         [C("agent", 20000, 0), C("report_enhance", 1000, 0)],
         "skip"),
    ]
    ok = True
    print("== 判据自检（构造数据）==")
    for name, calls, expect in cases:
        got = diagnose(calls)["verdict"]
        flag = "✅" if got == expect else "❌"
        ok &= got == expect
        print(f"  {flag} {name}")
        print(f"     期望={expect} 实际={got}")
    print("\n" + ("✅ 自检通过" if ok else "❌ 自检失败 —— 判据不可信"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="202610")
    ap.add_argument("--run", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    stats = defaultdict(int)
    results = []
    for p in _files([m.strip() for m in args.months.split(",") if m.strip()]):
        if not p.exists():
            continue
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rids = ([args.run] if args.run
                    else [r[0] for r in con.execute("SELECT id FROM runs")])
            for rid in rids:
                calls = _calls(con, rid)
                if not calls:
                    continue
                res = diagnose(calls)
                stats[res["verdict"]] += 1
                if res["verdict"] != "skip":
                    results.append({"run": rid, **res})
        finally:
            con.close()

    if args.json:
        print(json.dumps({"stats": dict(stats),
                          "results": results}, ensure_ascii=False, indent=2))
        return 1 if stats.get("alert") else 0

    print(f"扫描 {args.months}："
          f"健康 {stats.get('healthy', 0)} / 告警 {stats.get('alert', 0)} / "
          f"无埋点 {stats.get('no_marker', 0)} / 跳过 {stats.get('skip', 0)}\n")
    print(f"== 判据（看结果，不看谁夹在中间）==")
    print(f"  report-fast 命中率 ≥ {HEALTHY_HIT:.0%} → 健康（PRD-A11 AC-3 口径）")
    print(f"  < {HEALTHY_HIT:.0%} 且该 run 有 fork 埋点 → 告警，列出前一跳调用供定位")
    print(f"  无埋点（埋点上线前的历史数据）→ 只统计，不判定\n")

    alerts = [r for r in results if r["verdict"] == "alert"]
    if not alerts:
        healthy = [r for r in results if r["verdict"] == "healthy"]
        print(f"✅ 无告警（{len(healthy)} 个 run 的 report-fast 命中 ≥50%）")
        for r in sorted(healthy, key=lambda x: -x["body"]["hit"])[:6]:
            sp = " → ".join(f"{c['purpose']}({c['inp']})" for c in r["span"]) or "(直接相连)"
            print(f"   · {r['run'][:8]}  命中 {r['body']['hit']:.0%}  区间: {sp}")
        return 0

    print(f"❌ {len(alerts)} 个 run 的 report-fast 命中异常：\n")
    for r in alerts:
        print(f"  run {r['run'][:8]}  report-fast 命中 {r['body']['hit']:.0%}"
              f"（in={r['body']['inp']} cached={r['body']['ca']}）")
        print(f"    前一跳区间 {len(r['span'])} 次调用：")
        for c in r["span"]:
            mark = "✅fork" if c["fork_kind"] == "llm_fork" else "⚠️非fork"
            print(f"      {mark:<9}{c['purpose']:<22} in={c['inp']}")
        print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
# -*- coding: utf-8 -*-
"""图表链路离线回放：给定一个 trace run，回答「这份报告现在会有几张图、什么图」。

为什么需要它（2026-11-02 事故）：「报告零图」这条链路有三层，任何一层断了症状都一样
（正文里没有 @@CHART），只看报告根本分不清是哪层：

    ① 萃取层  tool_node_base → chart_extractor.extract_from_tool_output（工具返回 → fragment）
    ② 归集层  report_builder._merge_chart_markers（fragment → 报告尾部标记）
    ③ 兜底层  report_builder._maybe_build_tech_panels（本地 K 线 → 技术面图板）

本脚本把 ①②③ 在同一份真实工具输出上重跑一遍，逐条打印，最后给出「修复后这份报告会有
多少张图」。**全程写临时库，不碰 db/stock_radar.db**，可以随时对历史 run 复盘。

用法::

    .\\venv\\Scripts\\python.exe scripts/devtools/verify_chart_pipeline.py --run-id <rid>
    .\\venv\\Scripts\\python.exe scripts/devtools/verify_chart_pipeline.py            # 取最近一个 run

    # 只看萃取层（跳过图板探测，避免依赖本地 K 线库）
    ... --no-tech-panel
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _trace_db(month: str) -> str:
    return os.path.join(ROOT, "db", "agent_trace", f"agent_trace_{month}.db")


def _latest_run(conn) -> str:
    row = conn.execute(
        "SELECT run_id FROM steps WHERE step_type='graph' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else ""


def _tool_steps(conn, run_id: str) -> list:
    """取该 run 的工具步（工具名 + 输出 + 入参）。

    入参在 steps.input 里，可能被 blob 压缩；行工厂已透明解压。
    """
    rows = conn.execute(
        "SELECT step_name, status, output, input, extra FROM steps "
        "WHERE run_id=? AND step_type='tool' ORDER BY started_at", (run_id,)
    ).fetchall()
    out = []
    for r in rows:
        args = {}
        raw_in = r["input"] or ""
        try:
            parsed = json.loads(raw_in)
            if isinstance(parsed, dict):
                args = parsed.get("args") or parsed
        except Exception:
            pass
        out.append({"name": r["step_name"], "status": r["status"],
                    "output": r["output"] or "", "args": args})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回放一个 trace run 的图表链路")
    ap.add_argument("--run-id", default="", help="trace run id（默认取最近一个）")
    ap.add_argument("--month", default="", help="trace 库月份，如 202611（默认自动）")
    ap.add_argument("--no-tech-panel", action="store_true", help="跳过技术面图板探测")
    ap.add_argument("--limit", type=int, default=200, help="最多回放多少个工具步")
    args = ap.parse_args()

    from utils.agent_trace.blobstore import install_row_factory
    from agents.analysis import chart_extractor as ce
    from agents.analysis import report_builder as rb

    # ① 写临时图表库，绝不污染生产 fragment 表
    tmp_db = os.path.join(tempfile.mkdtemp(prefix="chart_verify_"), "chart.db")
    ce.get_chart_db_path = lambda: tmp_db

    months = [args.month] if args.month else ["202611", "202610", "202609"]
    conn = None
    for month in months:
        path = _trace_db(month)
        if not os.path.exists(path):
            continue
        conn = install_row_factory(sqlite3.connect(path))
        if args.run_id:
            hit = conn.execute("SELECT 1 FROM steps WHERE run_id=? LIMIT 1",
                               (args.run_id,)).fetchone()
            if hit:
                break
            conn = None
        else:
            break
    if conn is None:
        print(f"[x] 找不到 trace run: {args.run_id or '(自动)'}；可用月份 {months}")
        return 2

    run_id = args.run_id or _latest_run(conn)
    if not run_id:
        print("[x] trace 库里没有 run")
        return 2

    steps = _tool_steps(conn, run_id)[: args.limit]
    print(f"run_id = {run_id}")
    print(f"工具步 {len(steps)} 个；图表白名单 = {sorted(ce.CHART_TOOLS)}")
    print(f"临时图表库 = {tmp_db}")
    print("-" * 74)

    markers: list = []
    code_hint: list = []
    per_tool: Counter = Counter()
    for st in steps:
        args_txt = json.dumps(st["args"], ensure_ascii=False)
        code_hint.append(args_txt)
        if st["name"] not in ce.CHART_TOOLS:
            continue
        if st["status"] != "success":
            print(f"  [skip] {st['name']}: 工具失败（{st['output'][:60]}…）")
            continue
        ids = ce.extract_from_tool_output(st["name"], st["output"],
                                          tool_args=st["args"])
        per_tool[st["name"]] += len(ids)
        if ids:
            frags = [ce.get_fragment(i) for i in ids]
            print(f"  [ok]   {st['name']}: {len(ids)} 张 -> "
                  + "、".join(f"{f['title']}({f['chart_type']})" for f in frags if f))
        else:
            print(f"  [零图] {st['name']}: 输出未命中任何图表规则 "
                  f"（len={len(st['output'])}）")
        markers.extend(f"@@CHART:{i}@@" for i in ids)

    print("-" * 74)
    print("萃取层小结:", dict(per_tool) or "（没有可萃取的输出）")

    # ② + ③ 归集：模拟报告尾部
    body = "## 回放报告正文（占位）\n\n这段文字用来观察尾部图表标记。"
    if args.no_tech_panel:
        rb._tech_panel_max = lambda: 0
    merged = rb._merge_chart_markers(
        body, tool_evidence=" ".join(markers),
        code_hint="\n".join(code_hint))
    final = rb._CHART_MARK_RE.findall(merged)
    types = Counter()
    for m in final:
        frag = ce.get_fragment(m[8:-2])
        types[(frag or {}).get("chart_type", "?")] += 1
    print(f"最终报告标记数 = {len(final)}，按类型 {dict(types)}"
          f"（上限 _MAX_REPORT_CHARTS={rb._MAX_REPORT_CHARTS}）")
    for m in final:
        frag = ce.get_fragment(m[8:-2]) or {}
        print(f"  - {frag.get('title', '?')}  [{frag.get('chart_type')}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())

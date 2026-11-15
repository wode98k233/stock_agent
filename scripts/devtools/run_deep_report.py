"""M1 冒烟：跑一份长报告骨架（离线 stub 或真 LLM）。

用法：
  python scripts/devtools/run_deep_report.py --list
  python scripts/devtools/run_deep_report.py --universe 银行板块 --sections 3 --stub
  python scripts/devtools/run_deep_report.py --universe 银行板块 --sections 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.deep_report.runtime import DeepReportRuntime          # noqa: E402
from agents.deep_report.store import DeepReportStore              # noqa: E402
from agents.deep_report.templates import list_templates           # noqa: E402


def _logger():
    from utils.logger import RadarLogger
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return RadarLogger(logging.getLogger("deep_report"), "smoke")


def _safe_db():
    """DB 不可用时返回 None：长报告仍可跑，只是没有历史/进度可查。"""
    try:
        from agents.deep_report.db import get_db
        return get_db()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ DB 不可用，本次不落元数据: {e}", file=sys.stderr)
        return None


def _watch(rid: str, timeout_s: float = 900.0) -> int:
    """轮询 DB 事件（游标 = id），打印进度；终态或超时退出。"""
    import time as _time
    from agents.deep_report.db import get_db

    db = get_db()
    meta = db.get_report(rid)
    if not meta:
        print(f"未找到报告: {rid}", file=sys.stderr)
        return 2
    print(f"[watch] {rid}  {meta.get('title') or '-'}")
    last, started = 0, _time.time()
    while True:
        for ev in db.events_since(rid, last):
            last = ev["id"]
            data = ev.get("data") or {}
            if ev["type"] == "section":
                print(f"  [章节 {data.get('index')}/{data.get('total')}] "
                      f"{data.get('status')} {data.get('title') or ''}")
            elif ev["type"] == "subagent":
                print(f"  [取数 {data.get('index')}/{data.get('total')}] "
                      f"{data.get('slot')} → {data.get('status')} ({data.get('items')} 项)")
            elif ev["type"] == "progress":
                print(f"  [进度] {data.get('progress')}%")
            elif ev["type"] == "done":
                print(f"  [完成] {data.get('status')} 字数={data.get('words')}")
                return 0
            elif ev["type"] == "error":
                print(f"  [错误] {data.get('message')}")
                return 1
        cur = db.get_report(rid)
        if cur and cur.get("status") in ("done", "partial", "failed", "cancelled"):
            print(f"[watch] 结束: {cur['status']}")
            return 0 if cur["status"] in ("done", "partial") else 1
        if _time.time() - started > timeout_s:
            print("[watch] 超时退出", file=sys.stderr)
            return 1
        _time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="长报告 M1 冒烟")
    ap.add_argument("--list", action="store_true", help="列出可用模板后退出")
    ap.add_argument("--universe", default="银行板块", help="标的/板块")
    ap.add_argument("--request", default="", help="用户原话（影响大纲增强）")
    ap.add_argument("--template", default="deep_sector_finance")
    ap.add_argument("--sections", type=int, default=3, help="本次只写前 N 节（M1 冒烟用）")
    ap.add_argument("--stub", action="store_true", help="不调 LLM，用占位正文验证编排")
    ap.add_argument("--fetch", action="store_true", help="开启子代理取数（需真实工具与 Key）")
    ap.add_argument("--json", action="store_true", help="只输出最终 meta JSON")
    ap.add_argument("--history", action="store_true", help="列出最近的长报告（读 DB）")
    ap.add_argument("--watch", default="", help="盯某份报告的事件流（轮询 DB）")
    args = ap.parse_args()

    if args.history:
        from agents.deep_report.db import get_db
        rows = get_db().list_reports(50)
        if not rows:
            print("（暂无记录）")
        for r in rows:
            print(f"{r['rid']}  {r['status']:<10} {str(r['progress'] or 0):>3}%  "
                  f"{r['sections_done']}/{r['sections_total']} 节  {r['title'] or '-'}")
        return 0

    if args.watch:
        return _watch(args.watch)

    if args.list:
        print("\n".join(list_templates()))
        return 0

    llm = None
    log = None
    if not args.stub:
        from utils.llm_factory import get_report_llm
        log = _logger()
        llm = get_report_llm()

    events = []
    executor = None
    if args.fetch and not args.stub:
        from agents.deep_report.tools_provider import load_tools
        from agents.subagent.executor import SubagentExecutor

        whitelist = ("mx_data_query", "iwc_query", "iwc_search", "mx_search_news", "web_search")
        tools = load_tools(whitelist=whitelist)
        if not tools:
            print("⚠️ 工具装载为空，本次不取数（章节会写数据缺口）", file=sys.stderr)
        else:
            executor = SubagentExecutor(llm=llm, tools=tools,
                                        log=log, deadline_s=180.0)

    runtime = DeepReportRuntime(llm=llm, log=log, executor=executor,
                                fetch=bool(args.fetch and executor),
                                db=_safe_db(),
                                progress=lambda ev, p: events.append((ev, p)))
    meta = runtime.run(universe=args.universe, user_request=args.request,
                       template_id=args.template, max_sections=args.sections)

    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    store = DeepReportStore()
    rid = meta["rid"]
    d = store.dir_of(rid)
    print("\n=== 长报告 M1 结果 ===")
    print(f"rid      : {rid}")
    print(f"status   : {meta['status']}  stage={meta['stage']}  progress={meta['progress']}%")
    print(f"title    : {meta.get('title')}")
    print(f"字数     : {meta.get('words')}  节数: {meta.get('sections_done')}/{meta.get('sections_total')}")
    print(f"目录     : {d}")
    for name in ("report.md", "report.html", "outline.json"):
        p = d / name
        print(f"  - {name:14s} {'OK' if p.exists() else '缺失'}  {p.stat().st_size if p.exists() else 0} B")
    print(f"事件     : {len(events)} 条（events.jsonl）")
    return 0 if meta["status"] in ("done", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())

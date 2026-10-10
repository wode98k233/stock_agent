"""
选股雷达 - 调用链追踪 CLI

命令:
  ls       列出最近运行
  show     查看某次运行的完整追踪
  tree     树形骨架（只看流程，不看内容）
  stats    token 统计和成本分析
  search   搜索关键词
  export   导出为 JSON
"""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from utils.agent_trace.models import _TYPE_ICONS

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def _db_path(args) -> str:
    """显式 --db（单文件工具语义：export/import/单库直查）；默认当月月库。"""
    if args.db:
        return args.db
    try:
        from utils.agent_trace.store import store
        return store.current_month_db()
    except Exception:
        return "agent_trace.db"


def _store():
    from utils.agent_trace.store import store
    return store


@contextmanager
def _conn(db: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_ls(args):
    n = args.n or 30
    runs = _store().list_runs(limit=n, status=args.status or "")
    if not runs:
        print("无记录")
        return
    for r in runs:
        icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(r["status"], "?")
        ms = f"{r['duration_ms']:.0f}ms" if r["duration_ms"] else "—"
        print(f"  {icon} {r['id']}  {r['agent_name']:<16}  {ms:<10}  {r['created_at']}")


def cmd_show(args):
    st = _store()
    run_id = args.run_id
    run = st.get_run(run_id)
    if not run:
        print(f"未找到 run: {run_id}")
        return
    steps = st.get_steps(run_id)

    icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(run["status"], "?")
    ms = f"{run['duration_ms']:.0f}ms" if run["duration_ms"] else "—"
    print(f"{'='*70}")
    print(f"  Run: {run['id']}")
    print(f"  Agent: {run['agent_name']}  {icon}  {ms}")
    print(f"  Created: {run['created_at']}")
    if run["input"]:
        inp = run["input"][:200]
        print(f"  Input: {inp}")
    if run["error"]:
        print(f"  Error: {run['error'][:200]}")
    print(f"{'='*70}")

    for s in steps:
        s_icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(s["status"], "?")
        s_ms = f"{s['duration_ms']:.0f}ms" if s["duration_ms"] else "—"
        type_icon = _TYPE_ICONS.get(s["step_type"], "?")
        parent = f"← {s['parent_run_id'][:8]}" if s["parent_run_id"] else ""
        print(f"  {type_icon} {s['step_name']:<30} {s_icon} {s_ms}  {parent}")
        if args.verbose:
            if s["input"]:
                print(f"     IN:  {str(s['input'])[:120]}")
            if s["output"]:
                print(f"     OUT: {str(s['output'])[:120]}")
            if s["error"]:
                print(f"     ERR: {s['error'][:120]}")


def cmd_tree(args):
    st = _store()
    run_id = args.run_id
    verbose = getattr(args, "verbose", False)
    run = st.get_run(run_id)
    if not run:
        print(f"未找到 run: {run_id}")
        return
    steps = st.get_steps(run_id)

    # 把 sqlite3.Row 转换为 dict
    steps_dicts = [dict(s) for s in steps]
    run_dict = dict(run)

    icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(run_dict["status"], "?")
    ms = f"{run_dict['duration_ms']:.1f}s" if run_dict["duration_ms"] else "—"
    print(f"🌳 Run: {run_dict['id'][:8]}  Agent: {run_dict['agent_name']}  {icon} {ms}")
    print(f"{'─'*60}")

    children: dict = {}
    roots = []
    for s in steps_dicts:
        pid = s["parent_run_id"]
        if pid:
            children.setdefault(pid, []).append(s)
        else:
            roots.append(s)

    def _is_noise(step):
        """判断节点是否为噪音节点"""
        extra = step.get("extra")
        if not extra:
            return False
        try:
            d = json.loads(extra)
            return d.get("_noise", False)
        except (json.JSONDecodeError, TypeError):
            return False

    def _print_tree(nodes, prefix=""):
        for i, s in enumerate(nodes):
            is_last = i == len(nodes) - 1
            is_noise = _is_noise(s)

            # 如果是噪音节点且非 verbose 模式，直接递归打印子节点而跳过自身
            if is_noise and not verbose:
                kids = children.get(s["event_run_id"], [])
                if kids:
                    _print_tree(kids, prefix)
                continue

            # 正常打印当前节点
            connector = "└─ " if is_last else "├─ "
            s_icon = {"success": "✅", "error": "❌"}.get(s["status"], "⏳")
            s_ms = f"{s['duration_ms']:.0f}ms" if s["duration_ms"] else "—"
            type_icon = _TYPE_ICONS.get(s["step_type"], "?")
            
            noise_marker = "🔇 " if is_noise else ""
            print(f"{prefix}{connector}{type_icon} {noise_marker}{s['step_name']}  {s_icon} {s_ms}")
            ext = "   " if is_last else "│  "
            kids = children.get(s["event_run_id"], [])
            if kids:
                _print_tree(kids, prefix + ext)

    _print_tree(roots)


def cmd_stats(args):
    st = _store()
    n = args.n or 30
    runs = st.list_runs(limit=n)
    if not runs:
        print("无记录")
        return

    total = len(runs)
    ok = sum(1 for r in runs if r["status"] == "success")
    err = sum(1 for r in runs if r["status"] == "error")
    durations = [r["duration_ms"] for r in runs if r["duration_ms"]]
    avg_d = sum(durations) / len(durations) if durations else 0
    min_d = min(durations) if durations else 0
    max_d = max(durations) if durations else 0

    agents: dict = {}
    for r in runs:
        agents[r["agent_name"]] = agents.get(r["agent_name"], 0) + 1

    total_in = 0
    total_out = 0
    model_stats: dict = {}
    extras_by_run = st.llm_steps_totals([r["id"] for r in runs])
    for item in (it for lst in extras_by_run.values() for it in lst):
        extra = item.get("extra")
        if extra:
            try:
                d = json.loads(extra)
                from utils.token_usage import normalize_token_usage_dict
                tu = normalize_token_usage_dict(d.get("token_usage"))
                inp = tu["input_tokens"]
                out = tu["output_tokens"]
                total_in += inp
                total_out += out
                mn = item.get("step_name") or "unknown"
                if mn not in model_stats:
                    model_stats[mn] = {"calls": 0, "in": 0, "out": 0}
                model_stats[mn]["calls"] += 1
                model_stats[mn]["in"] += inp
                model_stats[mn]["out"] += out
            except (json.JSONDecodeError, TypeError):
                pass

    err_rate = err / total * 100 if total else 0
    print(f"📊 Stats (最近 {total} runs)")
    print(f"{'─'*50}")
    print(f"  总数: {total}  │  成功: {ok}  │  失败: {err}  │  错误率: {err_rate:.1f}%")
    print(f"  耗时  平均: {avg_d/1000:.1f}s  │  最快: {min_d/1000:.1f}s  │  最慢: {max_d/1000:.1f}s")
    print(f"  Agent 分布: {', '.join(f'{k}({v})' for k, v in agents.items())}")
    if total_in or total_out:
        print(f"\n  Token  总计: {total_in+total_out:,}  │  输入: {total_in:,}  │  输出: {total_out:,}")
        print(f"  按模型:")
        for mn, ms in model_stats.items():
            print(f"    {mn}: {ms['calls']} calls, {ms['in']:,} in / {ms['out']:,} out")


def cmd_search(args):
    rows = _store().search_runs(args.keyword, limit=20)
    if not rows:
        print("未找到匹配记录")
        return
    for r in rows:
        icon = {"success": "✅", "error": "❌"}.get(r["status"], "?")
        print(f"  {icon} {r['id'][:8]}  {r['agent_name']:<16}  {r['created_at'][:19]}")


def cmd_export(args):
    db = _db_path(args)
    if not Path(db).exists():
        print(f"数据库不存在: {db}")
        return
    run_id = args.run_id
    from utils.agent_trace.migrate import TraceExporter
    exporter = TraceExporter(db)
    try:
        out = exporter.export_run(run_id, output_path=args.output or None, db_path=db)
        print(f"已导出: {out}")
    except ValueError as e:
        print(str(e))


def cmd_import(args):
    db = _db_path(args)
    from utils.agent_trace.migrate import TraceImporter
    importer = TraceImporter(db)
    strategy = args.strategy or "skip"
    json_path = args.json_path
    if Path(json_path).is_dir():
        result = importer.import_batch(json_path, strategy, db_path=db)
        print(f"导入完成: {result['imported']} 成功, {result['skipped']} 跳过, {result['errors']} 失败")
    else:
        try:
            ok = importer.import_run(json_path, strategy, db_path=db)
            print(f"{'导入成功' if ok else '跳过(已存在)'}: {json_path}")
        except ValueError as e:
            print(f"导入失败: {e}")


def cmd_calendar(args):
    """按日期分组展示 run 的 token 统计和耗时（分库：经 calendar_query 逐月合并）。"""
    n = args.n or 30
    year = getattr(args, "year", None)
    month = getattr(args, "month", None)

    from utils.agent_trace.calendar_query import query_trace_calendar
    data = query_trace_calendar(_store(), year, month)
    run_days = data.get("run_days") or {}
    if not run_days:
        print("无记录")
        return

    # 按日期分组（query 已带 token 统计），倒序取最近 n 天
    from collections import OrderedDict
    days: OrderedDict[str, list] = OrderedDict()
    for day_str in sorted(run_days.keys(), reverse=True):
        days[day_str] = run_days[day_str]
    day_keys = list(days.keys())[:n]

    # 输出
    print(f"📅 Trace Calendar (最近 {len(day_keys)} 天)")
    print(f"{'═'*80}")

    for dk in day_keys:
        runs = days[dk]
        total = len(runs)
        ok = sum(1 for r in runs if r["status"] == "success")
        err = sum(1 for r in runs if r["status"] == "error")

        day_in = 0
        day_out = 0
        day_total = 0
        day_calls = 0
        durations = []
        for r in runs:
            day_in += r.get("input_tokens", 0)
            day_out += r.get("output_tokens", 0)
            day_total += r.get("total_tokens", 0)
            day_calls += r.get("llm_calls", 0)
            if r.get("duration_ms"):
                durations.append(r["duration_ms"])

        avg_d = sum(durations) / len(durations) if durations else 0
        status_str = f"✅{ok}"
        if err:
            status_str += f" ❌{err}"

        token_str = ""
        if day_total > 0:
            token_str = f"  🔤 {day_in:,} in / {day_out:,} out / {day_total:,} total ({day_calls} calls)"

        dur_str = ""
        if avg_d > 0:
            dur_str = f"  ⏱ avg {avg_d/1000:.1f}s"

        print(f"\n  📆 {dk}  │ {total} runs │ {status_str}{token_str}{dur_str}")

        # 展示每条 run
        for r in runs:
            icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(r["status"], "?")
            ms = f"{r['duration_ms']/1000:.1f}s" if r.get("duration_ms") else "—"
            tok = f"{r.get('total_tokens', 0):,} tok" if r.get("total_tokens") else ""
            agent = r["agent_name"][:20]
            rid = r["id"][:8]
            print(f"    {icon} {rid}  {agent:<20} {ms:<8} {tok}")


def cmd_calendar_json(args) -> dict:
    """返回日历统计的 JSON 数据（供 API 使用）。"""
    year = getattr(args, "year", None)
    month = getattr(args, "month", None)

    from utils.agent_trace.calendar_query import query_trace_calendar
    return query_trace_calendar(_store(), year, month)


def cmd_cleanup(args):
    """清理过期月库（保留最近 N 个月；无 --apply 时仅预览）。"""
    from utils.agent_trace import cleanup as trace_cleanup

    keep = args.keep_months
    plan = trace_cleanup.preview(keep)
    if not plan.months:
        print(f"无可清理月库（现有月份：{store_months() or '无'}，保留 {keep} 个月）")
        return
    print(f"将清理 {len(plan.months)} 个月库（保留最近 {keep} 个月）：")
    for m in plan.months:
        print(f"  {m.month}: {m.runs} runs / {m.steps} steps / {m.messages} messages "
              f"→ {m.db_path}（{m.size_mb} MB）")
    print(f"合计释放约 {plan.total_size_mb} MB、{plan.total_runs} 个 run")
    if not args.apply:
        print("预览完成（未删除）。加 --apply 正式执行。")
        return
    result = trace_cleanup.apply(plan)
    print(f"已删除 {len(result['removed_months'])} 个月库，"
          f"释放 {result['freed_mb']} MB，级联清理 reviews {result['reviews_deleted']} 条")


def store_months() -> list:
    try:
        return _store().iter_existing_months()
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser(
        prog="agent_trace",
        description="选股雷达 - 调用链追踪 CLI",
    )
    parser.add_argument("--db", default="", help="数据库路径（默认使用 app_paths）")
    sub = parser.add_subparsers(dest="command")

    p_ls = sub.add_parser("ls", help="列出最近运行")
    p_ls.add_argument("-n", type=int, default=30, help="显示条数")
    p_ls.add_argument("--status", default="", help="按状态过滤: success/error/running")

    p_show = sub.add_parser("show", help="查看某次运行的完整追踪")
    p_show.add_argument("run_id", help="Run ID")
    p_show.add_argument("-v", "--verbose", action="store_true", help="显示输入输出")

    p_tree = sub.add_parser("tree", help="树形骨架")
    p_tree.add_argument("run_id", help="Run ID")
    p_tree.add_argument("-v", "--verbose", action="store_true", help="显示所有节点（包括噪音节点）")

    p_stats = sub.add_parser("stats", help="token 统计")
    p_stats.add_argument("-n", type=int, default=30, help="统计最近 N 条")

    p_search = sub.add_parser("search", help="搜索关键词")
    p_search.add_argument("keyword", help="搜索关键词")

    p_export = sub.add_parser("export", help="导出为 JSON")
    p_export.add_argument("run_id", help="Run ID")
    p_export.add_argument("-o", "--output", default="", help="输出文件路径")

    p_import = sub.add_parser("import", help="导入 trace JSON")
    p_import.add_argument("json_path", help="JSON 文件路径或目录")
    p_import.add_argument("--strategy", default="skip", choices=["skip", "replace", "error"],
                          help="冲突处理策略 (默认: skip)")

    p_cal = sub.add_parser("calendar", help="按日期查看 token 统计")
    p_cal.add_argument("-n", type=int, default=30, help="显示天数")
    p_cal.add_argument("--year", type=int, default=None, help="过滤年份")
    p_cal.add_argument("--month", type=int, default=None, help="过滤月份")

    p_cleanup = sub.add_parser("cleanup", help="清理过期月库（默认保留 6 个月）")
    p_cleanup.add_argument("--keep-months", type=int, default=6, help="保留最近 N 个月（默认 6）")
    p_cleanup.add_argument("--apply", action="store_true", help="正式删除（默认仅预览）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmds = {
        "ls": cmd_ls,
        "show": cmd_show,
        "tree": cmd_tree,
        "stats": cmd_stats,
        "search": cmd_search,
        "export": cmd_export,
        "import": cmd_import,
        "calendar": cmd_calendar,
        "cleanup": cmd_cleanup,
    }
    fn = cmds.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

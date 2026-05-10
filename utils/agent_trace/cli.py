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
    if args.db:
        return args.db
    try:
        from utils.app_paths import get_trace_db_path
        return get_trace_db_path()
    except Exception:
        return "agent_trace.db"


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
    db = _db_path(args)
    if not Path(db).exists():
        print(f"数据库不存在: {db}")
        return
    n = args.n or 30
    where = ""
    params: list = []
    if args.status:
        where = "WHERE status=?"
        params.append(args.status)
    with _conn(db) as c:
        rows = c.execute(
            f"SELECT id,agent_name,status,created_at,duration_ms "
            f"FROM runs {where} ORDER BY created_at DESC LIMIT ?",
            params + [n],
        ).fetchall()
    if not rows:
        print("无记录")
        return
    for r in rows:
        icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(r["status"], "?")
        ms = f"{r['duration_ms']:.0f}ms" if r["duration_ms"] else "—"
        print(f"  {icon} {r['id']}  {r['agent_name']:<16}  {ms:<10}  {r['created_at']}")


def cmd_show(args):
    db = _db_path(args)
    if not Path(db).exists():
        print(f"数据库不存在: {db}")
        return
    run_id = args.run_id
    with _conn(db) as c:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            print(f"未找到 run: {run_id}")
            return
        steps = c.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()

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
    db = _db_path(args)
    if not Path(db).exists():
        print(f"数据库不存在: {db}")
        return
    run_id = args.run_id
    verbose = getattr(args, "verbose", False)
    with _conn(db) as c:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            print(f"未找到 run: {run_id}")
            return
        steps = c.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()

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
    db = _db_path(args)
    if not Path(db).exists():
        print(f"数据库不存在: {db}")
        return
    n = args.n or 30
    with _conn(db) as c:
        runs = c.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (n,)
        ).fetchall()
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

        run_ids = [r["id"] for r in runs]
        placeholders = ",".join("?" * len(run_ids))
        steps = c.execute(
            f"SELECT * FROM steps WHERE run_id IN ({placeholders}) AND step_type='llm'",
            run_ids,
        ).fetchall()

        total_in = 0
        total_out = 0
        model_stats: dict = {}
        for s in steps:
            extra = s["extra"]
            if extra:
                try:
                    d = json.loads(extra)
                    tu = d.get("token_usage", {})
                    inp = tu.get("prompt_tokens", 0) or 0
                    out = tu.get("completion_tokens", 0) or 0
                    total_in += inp
                    total_out += out
                    mn = s["step_name"]
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
    db = _db_path(args)
    if not Path(db).exists():
        print(f"数据库不存在: {db}")
        return
    keyword = f"%{args.keyword}%"
    with _conn(db) as c:
        rows = c.execute(
            "SELECT id,agent_name,status,created_at FROM runs "
            "WHERE input LIKE ? OR output LIKE ? OR error LIKE ? "
            "ORDER BY created_at DESC LIMIT 20",
            (keyword, keyword, keyword),
        ).fetchall()
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
    with _conn(db) as c:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            print(f"未找到 run: {run_id}")
            return
        steps = c.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        step_ids = [s["id"] for s in steps]
        messages = {}
        if step_ids:
            placeholders = ",".join("?" * len(step_ids))
            for m in c.execute(
                f"SELECT * FROM messages WHERE step_id IN ({placeholders}) ORDER BY id",
                step_ids,
            ).fetchall():
                messages.setdefault(m["step_id"], []).append(dict(m))

    data = {
        "run": dict(run),
        "steps": [dict(s) for s in steps],
        "messages": messages,
    }
    out = args.output or f"trace_{run_id[:8]}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"已导出: {out}")


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
    }
    fn = cmds.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

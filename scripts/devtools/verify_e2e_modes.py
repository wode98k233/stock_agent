# -*- coding: utf-8 -*-
"""端到端验证：同一问题在多个 agent 模式下各跑一次，然后**逐 step 对照**（PRD-A11 验收）。

走的是与 Web 完全同一条路径（agent factory → SkillRegistry → agent.run），
所以 trace 里的形态就是生产形态。

用法：
    # 默认跑 react_stock + unified_plan，问题用药明康德
    python scripts/devtools/verify_e2e_modes.py
    python scripts/devtools/verify_e2e_modes.py "获取药明康德 最新股价和一条相关新闻"
    python scripts/devtools/verify_e2e_modes.py --modes react_stock,unified_plan
    python scripts/devtools/verify_e2e_modes.py --no-run     # 只看最近一次 run 的对照

⚠️ 不带 --no-run 会真实调用 LLM（每题每模式一次完整 run）。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_QUERY = "获取药明康德 最新股价和一条相关新闻"
DEFAULT_MODES = ["react_stock", "unified_plan"]


async def _run_one(mode: str, query: str) -> dict:
    from utils.bootstrap_common import init_common
    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from agents import register_all
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry

    common = init_common()
    logger, *_ = get_logger(query)
    memory = MemoryManager(logger)
    try:
        from memory.sdk import get_sdk
        get_sdk()
    except Exception:
        pass

    register_all()
    agent = AgentFactory.get(mode)
    agent.on_startup()
    registry = SkillRegistry(logger, memory, common.skill_register)

    print(f"\n{'=' * 70}\n[run] mode={mode}\n[run] query={query}\n{'=' * 70}", flush=True)
    t0 = time.time()
    resp = await agent.run(query, registry, memory, logger)
    dur = time.time() - t0
    text = getattr(resp, "response", None) or getattr(resp, "content", None) or str(resp)
    print(f"[run] 完成，用时 {dur:.0f}s，回答 {len(text)} 字", flush=True)
    return {"mode": mode, "query": query, "seconds": dur, "chars": len(text)}


def _analyze(mode_query: dict, run_ids: list[str]) -> None:
    """逐 step 对照：命中率 / fork 打点 / 压缩 / 报告链路顺序。"""
    import sqlite3

    trace_dir = ROOT / "db" / "agent_trace"
    dbs = sorted(trace_dir.glob("agent_trace_*.db"))

    for rid in run_ids:
        rows = []
        for db in dbs:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            got = list(con.execute(
                "SELECT id, run_id, step_name, started_at, duration_ms, extra "
                "FROM steps WHERE run_id=? ORDER BY id", (rid,)))
            con.close()
            if got:
                rows = got
                break
        if not rows:
            print(f"\n⚠️ 找不到 run {rid}")
            continue

        print(f"\n{'=' * 96}")
        print(f"run {rid[:12]}  共 {len(rows)} 个 step")
        print("=" * 96)
        print(f"{'#':>3} {'step':<26}{'purpose':<22}{'in':>7}{'ca':>7}{'hit':>7}"
              f"{'out':>6}{'reason':>7}{'fork':>12}{'ms':>7}")
        tot = {"in": 0, "ca": 0, "out": 0}
        for i, r in enumerate(rows, 1):
            e = json.loads(r["extra"] or "{}")
            tu = e.get("token_usage") or {}
            if not tu:
                continue
            inp = int(tu.get("input_tokens") or 0)
            ca = int(tu.get("cached_tokens") or 0)
            out = int(tu.get("output_tokens") or 0)
            rz = int(tu.get("reasoning_tokens") or 0)
            tot["in"] += inp
            tot["ca"] += ca
            tot["out"] += out
            hit = f"{ca / inp:.0%}" if inp else "-"
            fk = str(e.get("fork_kind") or "")
            if fk == "llm_fork":
                fk = f"fork({e.get('fork_prefix_msgs', '?')}m)"
            reason = str(e.get("compress_reason") or "")
            print(f"{i:>3} {str(r['step_name'])[:25]:<26}"
                  f"{str(e.get('purpose') or e.get('node') or '-')[:21]:<22}"
                  f"{inp:>7}{ca:>7}{hit:>7}{out:>6}{reason[-6:]:>7}{fk[:11]:>12}"
                  f"{int(r['duration_ms'] or 0):>7}")
        if tot["in"]:
            print(f"{'':>3} {'合计':<26}{'':<22}{tot['in']:>7}{tot['ca']:>7}"
                  f"{tot['ca'] / tot['in']:>7.1%}{tot['out']:>6}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=DEFAULT_QUERY)
    ap.add_argument("--modes", default=",".join(DEFAULT_MODES))
    ap.add_argument("--no-run", action="store_true", help="不调用 LLM，只对照最近两个 run")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    results = []
    if not args.no_run:
        for m in modes:
            results.append(asyncio.run(_run_one(m, args.query)))

    # 取最近两个 run（或刚跑完的）
    import sqlite3
    trace_dir = ROOT / "db" / "agent_trace"
    dbs = sorted(trace_dir.glob("agent_trace_*.db"), reverse=True)
    run_ids: list[tuple[str, str]] = []
    for db in dbs:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT id, agent_name, created_at FROM runs ORDER BY rowid DESC LIMIT 8"):
            if r["agent_name"] in modes:
                run_ids.append((r["id"], r["agent_name"]))
        con.close()
        if run_ids:
            break
    # 每个模式取最新一个
    picked = {}
    for rid, name in run_ids:
        picked.setdefault(name, rid)
    print(f"\n\n{'#' * 96}\n# 逐 step 对照（每模式取最新 run）\n{'#' * 96}")
    _analyze({}, [picked[m] for m in modes if m in picked])
    print("\n提示：顺序守卫跑 `python scripts/devtools/verify_report_fork.py order "
          f"--run {list(picked.values())[0] if picked else '<run_id>'}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
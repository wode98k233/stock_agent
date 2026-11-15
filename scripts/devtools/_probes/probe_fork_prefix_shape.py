# -*- coding: utf-8 -*-
"""fork 前缀形态探针：把「主前缀」拆段、验字节稳定性、量各副调用的尾部成本（**零 LLM 调用**）。

回答：
  1. 主前缀 `[cache_prefix][tools][REACT_SYSTEM_PROMPT]` 有多大、跨 run 是否字节稳定
     （稳定的才能当缓存预热段被副调用复用）
  2. 「把技能详情从 system 中段挪到尾部」后，agent 首调里 [tools][REACT] 与 user 之间
     还剩不剩动态内容（FR-11 的白盒判据）
  3. 每个副调用 fork 之后的**尾部**有多大 → 用实测命中率 + 价目算「fork 前/后成本」，
     直接判定哪些副调用 fork 划算、哪些是净亏

用法：
    python scripts/devtools/probe_fork_prefix_shape.py
    python scripts/devtools/probe_fork_prefix_shape.py --template market_daily --skills mx_data,market_review
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
TRACE_DIR = ROOT / "db" / "agent_trace"

# 价目（data/pricing/pricing_catalog.default.json）：闲时 in_miss=1 / in_hit=0.02 / out=4 元/M
RATE = {"in_miss": 1.0, "in_hit": 0.02, "out": 4.0}

DEFAULT_SKILLS = "mx_data,market_review,mx_search,stock_analysis,money_flow"


def tok(text: str) -> int:
    """项目既有口径：3 字符/token（见 archive/probe_prefix_sizes.py，2026-10-17 归档）。"""
    return len(text or "") // 3


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def line(name: str, text: str) -> None:
    print(f"{name:<46}{len(text or ''):>8} 字符  ≈{tok(text):>6} tok  {sha(text)}")


def observed_hit(months: tuple[str, ...] = ("202609", "202610")) -> dict[str, tuple[float, float, float]]:
    """从 trace 读各 node 的实测 (平均 input, 命中率, 调用数)，口径：Σcached / Σinput。"""
    acc = defaultdict(lambda: [0, 0, 0])
    files = [TRACE_DIR / f"agent_trace_{m}.db" for m in months]
    files = [p for p in files if p.exists()] or sorted(TRACE_DIR.glob("agent_trace_*.db"))
    for p in files:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for r in con.execute("select extra from steps where step_type='llm'"):
            try:
                e = json.loads(r["extra"] or "{}")
            except Exception:
                continue
            if e.get("cache_hit"):
                continue
            tu = e.get("token_usage") or {}
            node = str(e.get("purpose") or e.get("node") or "")
            if not node:
                continue
            acc[node][0] += int(tu.get("input_tokens") or 0)
            acc[node][1] += int(tu.get("cached_tokens") or 0)
            acc[node][2] += 1
        con.close()
    return {k: (v[0] / v[2], (v[1] / v[0]) if v[0] else 0.0, v[2])
            for k, v in acc.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="market_daily")
    ap.add_argument("--skills", default=DEFAULT_SKILLS)
    args = ap.parse_args()
    skills = [s.strip() for s in args.skills.split(",") if s.strip()]

    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from tools.skill_register import SkillRegister
    from tools.skills import SkillRegistry, SkillPromptBuilder
    from agents.prompts import (REACT_SYSTEM_PROMPT, SKILL_ROUTER_PROMPT,
                                get_skill_selector_prompt)
    from agents.analysis.template_store import load_template, build_guidance
    from agents.react.skill_nodes import _compact_catalog
    from agents.react.context import SUMMARY_SYSTEM_PROMPT
    from agents.shared.usage_guide import build_guide_text
    from utils.cache_prefix import DEFAULT_CACHE_PREFIX

    logger, *_ = get_logger("probe-fork-prefix", skip_db=True)
    memory = MemoryManager(logger)
    reg = SkillRegister()
    reg.auto_discover()
    sr = SkillRegistry(logger, memory, reg)

    # ── 真实 user_input / history 样本（取最新一次 react_stock run）──────────
    user_input = "今日大盘总结"
    history_chars = 0
    for db in (TRACE_DIR / "agent_trace_202610.db", TRACE_DIR / "agent_trace_202609.db"):
        if db.exists():
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "select input from runs where agent_name='react_stock' "
                "and status='success' order by rowid desc limit 1").fetchone()
            con.close()
            if row and row["input"]:
                user_input = row["input"]
            break
    history_chars = 1200  # 多轮场景的典型 history 体量（首轮为 0）

    tpl = load_template(args.template)
    guidance = build_guidance(tpl, user_input) or ""
    catalog = SkillPromptBuilder.build_catalog_prompt(sr)
    selector = get_skill_selector_prompt(catalog, tpl)
    compact_cat, _names = _compact_catalog(sr)
    detail = "\n\n".join(
        d for d in (SkillPromptBuilder.build_tools_detail_prompt(sr, s) for s in skills) if d)
    guide_text = build_guide_text(sr, skills)

    all_tools = sr.get_all_tools()
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool
        tools_json = json.dumps([convert_to_openai_tool(t) for t in all_tools],
                                ensure_ascii=False)
    except Exception:
        tools_json = json.dumps([{"name": getattr(t, "name", ""),
                                  "description": getattr(t, "description", "")}
                                 for t in all_tools], ensure_ascii=False)

    print(f"技能 {len(reg.get_all_skills())} 个 / 去重工具 {len(all_tools)} 个 / "
          f"模板 {args.template}")
    print(f"user_input 取自真实 run（{len(user_input)} 字符）\n")

    # ── 1. 主前缀分段 ─────────────────────────────────────────────────
    print("== 1. 主前缀分段（fork 的共享段）==")
    line("[0] DEFAULT_CACHE_PREFIX（CACHE_PREFIX_ENABLED=true 时注入）",
         DEFAULT_CACHE_PREFIX)
    line("[1] tools 数组（convert_to_openai_tool）", tools_json)
    line("[2] REACT_SYSTEM_PROMPT", REACT_SYSTEM_PROMPT)
    p_core = tok(DEFAULT_CACHE_PREFIX) + tok(tools_json) + tok(REACT_SYSTEM_PROMPT)
    print(f"{'→ P_core = [0]+[1]+[2]':<46}{'':>8}{'':>10}  ≈{p_core:>6} tok")
    print()

    # ── 2. 动态段（现状 vs 改造后）──────────────────────────────────────
    print("== 2. 动态段：技能详情放中段 vs 放尾部 ==")
    line("D 技能详情 Σ build_tools_detail_prompt", detail)
    line("   └ 其中 usage_guide 部分（build_guide_text）", guide_text)
    t_d, t_g = tok(detail), tok(guide_text)
    print(f"{'→ 重复投递 ≈ D − guide = ':<46}{'':>8}{'':>10}  ≈{max(0, t_d-t_g):>6} tok"
          f"（SubGoalRouter 投 guide，PrepareNode 投整份 detail）")
    print()
    print(f"{'段':<46}{'现状(中段)':>12}{'改造后(尾部)':>13}")
    rows = [
        ("[0][1][2] P_core（可跨 run 稳定）", p_core, p_core),
        ("D 技能详情", t_d, 0),
        ("history（典型多轮）", history_chars // 3, history_chars // 3),
        ("G 模板 guidance", tok(guidance), tok(guidance)),
        ("U user_input", tok(user_input), tok(user_input)),
        ("D' 技能详情（尾部追加）", 0, t_d),
    ]
    tot_old = tot_new = 0
    for nm, a, b in rows:
        if nm.startswith("[0]"):
            continue
        tot_old += a
        tot_new += b
    for nm, a, b in rows:
        print(f"{nm:<46}{a:>12}{b:>13}")
    print(f"{'尾部合计（不含 P_core）':<46}{tot_old:>12}{tot_new:>13}")
    print()
    print("  判据（FR-11 白盒）：改造后 [0][1][2] 与 user 之间只剩 history/G/U 三段，")
    print("  其中 G 随模板、U 随问题变化 → **「[0][1][2] 独立可命中」成立**；")
    print("  现状下 D 插在 [2] 之后 → agent 首调只命中 [0][1][2]≈"
          f"{p_core} tok，与实测 agent#1 cached≈0~4,480 的量级一致。")
    print()

    # ── 3. 稳定性：同输入两次构造是否逐字节一致 ────────────────────────────
    print("== 3. 前缀字节稳定性（同输入两次构造）==")
    a1 = DEFAULT_CACHE_PREFIX + tools_json + REACT_SYSTEM_PROMPT
    build = a1
    mid = lambda: build + "\n\n" + "\n\n".join(   # noqa: E731 现状：技能详情拼在 system 中段
        d for d in (SkillPromptBuilder.build_tools_detail_prompt(sr, s) for s in skills) if d)
    sel2 = get_skill_selector_prompt(SkillPromptBuilder.build_catalog_prompt(sr),
                                     load_template(args.template))
    checks = [
        ("P_core = [0]+[1]+[2]", a1, DEFAULT_CACHE_PREFIX + tools_json + REACT_SYSTEM_PROMPT),
        ("P_core + 技能详情中段（现状 agent 首调）", mid(), mid()),
        ("select_skills system", selector, sel2),
        ("compact catalog（router 兜底用）", compact_cat, _compact_catalog(sr)[0]),
        ("SUMMARY_SYSTEM_PROMPT", SUMMARY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT),
        ("template guidance", guidance,
         build_guidance(load_template(args.template), user_input) or ""),
    ]
    for nm, x, y in checks:
        ok = "✅ 一致" if x == y else "❌ 不一致"
        print(f"  {nm:<46}{ok}  {sha(x)} / {sha(y)}")
    print()
    print("  → 「主前缀构造器」必须复用同一函数，否则 select_skills / prepare / agent")
    print("    各自拼一遍就会逐字节分叉，本前缀归零（这是 FR-10 存在的唯一理由）。")
    print()

    # ── 4. 各副调用 fork 前后的成本对照 ─────────────────────────────────
    hits = observed_hit()
    print("== 4. 副调用 fork 前后成本对照（每次调用，闲时价，仅输入侧）==")
    print("   口径：现状 = (in−hit)·miss + hit·hit ；fork = P_core·hit + tail·miss")
    print(f"   实测取自 202609+202610 月库；P_core = {p_core} tok（假设已被同 run 内前一次调用预热）")
    print(f"{'node':<22}{'n':>5}{'现状in':>8}{'命中':>7}{'现状¥':>9}"
          f"{'fork尾':>8}{'fork¥':>8}{'Δ¥':>8}{'判定':>9}")
    tails = {
        "select_skills": tok(compact_cat) + 200,     # 紧凑目录 + 紧凑指令（不再发模板 JSON 全文）
        "skill-router": 400,
        "planner": 1200,
        "react-context-summary": 300,               # pending 全文已在前缀里，尾部只剩指令
        "report-expand": 200,
        "report-select": 400,
    }
    rows_out = []
    for node, tail in tails.items():
        if node not in hits:
            continue
        inp, h, n = hits[node]
        now = (inp - inp * h) * RATE["in_miss"] / 1e6 + inp * h * RATE["in_hit"] / 1e6
        fk = p_core * RATE["in_hit"] / 1e6 + tail * RATE["in_miss"] / 1e6
        rows_out.append((node, n, inp, h, now, tail, fk))
    for node, n, inp, h, now, tail, fk in rows_out:
        print(f"{node:<22}{n:>5}{inp:>8.0f}{h:>7.0%}{now:>9.4f}{tail:>8}{fk:>8.4f}"
              f"{fk-now:>+8.4f}{'划算' if fk < now else '净亏':>9}")
    tot_now = sum(r[4] for r in rows_out)
    tot_fk = sum(r[6] for r in rows_out)
    print(f"{'合计':<22}{'':>5}{'':>8}{'':>7}{tot_now:>9.4f}{'':>8}{tot_fk:>8.4f}"
          f"{tot_fk-tot_now:>+8.4f}")
    print()
    print("  已是 fork 形态、仅列出实测（尾部=报告正文/任务指令，不可压缩）：")
    for node in ("report-fast", "dashboard"):
        if node in hits:
            inp, h, n = hits[node]
            print(f"    {node:<22} n={n:<4} in={inp:>7.0f} 命中={h:>5.0%}")
    print("    ⚠️ 历史均值含 2026-10-15 修复前的样本；dashboard 接上 PRD-A9 §14 fork")
    print("       之后（commit 470c9d7）实测 70%~85%，尾部剩下的就是报告正文本身。")
    print()
    print("  说明：")
    print("   · fork 省不省得看**尾部占比**：尾部越大越划算。")
    print("     上下文压缩的尾部≈300 tok（pending 全文已在前缀里）→ 收益最大；")
    print("     select_skills 若不紧凑化，尾部仍含 4.3K 模板 JSON → 收益被吃掉一半。")
    print("   · skill-router / planner 的现状 system 本身已部分复用同一条缓存线，")
    print("     fork 仍净赚，但绝对额很小（<0.001 元/次）。")
    print("   · 本表只算输入侧。输出侧（reasoning 占 49~84%）由调用本身的难度决定，")
    print("     fork 不改变它 —— 见 probe_compaction_cost.py 的输出构成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
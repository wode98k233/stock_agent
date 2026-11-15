# -*- coding: utf-8 -*-
"""只读探针：量化「agent 实际调用的工具里，有多少属于未被选中的技能」= 没拿到使用指南。

问题
----
TOOL_SURFACE_MODE=full（常量）→ 52 个工具常驻，agent 随时能调任何工具；而技能的
**使用指南是按 loaded_skills 选择性注入**的（agents/react/nodes.py::PrepareNode →
tools/skills.py::SkillPromptBuilder.build_tools_detail_prompt）。router
（sub_goal_router / reselect）本该在换批时更新这批指南，但实测永不触发
（skill_nodes.py::make_route_after_tools 的 len(loaded_skills) < len(candidate_skills)
恒 False，因为 select_skills 一次把候选池载满）。

所以：**agent 调了未被选中技能的工具 = 它在没有使用指南的情况下调用**。

本探针**不跑 LLM**，只做两件事：
1. 建「工具 → 所属技能」映射，并用两种互相独立的来源交叉验证；
2. 逐 run 读 trace，统计每个工具调用是否落在该 run 的技能详情消息里。

判据（刻意避开两个已知陷阱）
--------------------------
* 不用「工具名是技能详情文本的子串」当判据 —— 会把说明文字里顺带提到的名字算成
  「有指南」，也会因为投递位置不同（mid / tail）整体判错。
* 判据 = **整行等于 "### <tool_name>"**（当前 build_tools_detail_prompt 的工具条目形态）。
* 不把「注册表建不出来 → 映射为空 → 未覆盖=0」当结论 —— 映射为空直接 BLOCKED。

映射来源
--------
* 来源 B（主）：直接正则解析 tools/skills | other_skills | user_skills 下所有
  SKILL.md 的「## 工具列表」契约段（"### 工具N：<tool_name>"，由
  scripts/ops/patch_iwencai_skill_contracts.py 维护的格式）。
* 来源 A（交叉验证）：tools.skill_register.SkillRegister().auto_discover(fast=True)
  的注册表（走 utils.app_paths 解析技能目录；fast=True 不 import 任何技能模块）。
* 来源 C（实测）：trace 里 select_skills 步落盘的 all_tool_names，即 agent 当时
  真正能调的工具面 —— 用来检查映射是否覆盖真实工具。

只读保证：所有 sqlite 连接走 file:...?mode=ro；不写任何文件（--json 除外，显式指定才写）。

用法::

    python scripts/devtools/probe_skill_guide_coverage.py
    python scripts/devtools/probe_skill_guide_coverage.py --sample 3
    python scripts/devtools/probe_skill_guide_coverage.py --json out.json
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# 控制台按 UTF-8 输出（Windows 默认 GBK 会让中文/数学符号直接抛 UnicodeEncodeError）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_GLOB = str(REPO_ROOT / "db" / "agent_trace" / "agent_trace_*.db")
SKILL_ROOTS = ("tools/skills", "tools/other_skills", "user_skills")

# ── 技能详情消息的形态判据（当前代码形态）─────────────────────────────
# 第一行 "## 技能 <skill> 的工具详情"（build_tools_detail_prompt 的首行）
DETAIL_HEADER_RE = re.compile(r"^##\s*技能\s+(\S+)\s+的工具详情\s*$", re.M)
# 工具条目：整行 "### <tool_name>"（当前形态）。
# 注意：不是 "### 使用指南" / "### 工具1：xxx" 这类小标题。
TOOL_ENTRY_RE = re.compile(r"^###\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.M)
# strict 工具面残留标记：full 档下 PrepareNode 不再拼这段
LEGACY_SURFACE_RE = re.compile(r"^##\s*可用工具\s*$", re.M)

# SKILL.md 契约段解析（独立于 tools/skill_register.py 的实现）
MD_TOOL_RE = re.compile(r"^###\s*工具\d+[：:]\s*(\S+)\s*$", re.M)
MD_SECTION_RE = re.compile(r"^##\s*工具列表\s*$", re.M)
MD_NEXT_SECTION_RE = re.compile(r"^##\s", re.M)
MD_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.M)


def _connect_ro(path: str) -> sqlite3.Connection:
    """只读打开 sqlite（WAL 库也支持）；失败才退回普通连接（仍只 SELECT）。"""
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("select 1 from sqlite_master limit 1").fetchone()
        return conn
    except sqlite3.Error:
        return sqlite3.connect(path)


# ─────────────────────────────────────────────────────────────────────
# 1. 映射：工具 → 所属技能
# ─────────────────────────────────────────────────────────────────────
def mapping_from_skill_md():
    """来源 B：直接解析 SKILL.md 的「## 工具列表」契约段。"""
    tool2skills = {}
    skill2tools = {}
    files = []
    for root in SKILL_ROOTS:
        files += glob.glob(str(REPO_ROOT / root / "**" / "SKILL.md"), recursive=True)
    for path in sorted(files):
        try:
            text = open(path, encoding="utf-8-sig", errors="ignore").read()
        except OSError:
            continue
        head = text.split("---", 2)
        front = head[1] if text.startswith("---") and len(head) >= 3 else ""
        name_m = MD_NAME_RE.search(front)
        skill = (name_m.group(1).strip() if name_m
                 else os.path.basename(os.path.dirname(path))).replace("-", "_")
        sec = MD_SECTION_RE.search(text)
        if not sec:
            skill2tools.setdefault(skill, [])
            continue
        rest = text[sec.end():]
        nxt = MD_NEXT_SECTION_RE.search(rest)
        body = rest[: nxt.start()] if nxt else rest
        tools = MD_TOOL_RE.findall(body)
        skill2tools[skill] = tools
        for t in tools:
            tool2skills.setdefault(t, set()).add(skill)
    return tool2skills, skill2tools, files


def mapping_from_registry():
    """来源 A：SkillRegister 注册表（走 utils.app_paths 的技能目录解析）。"""
    sys.path.insert(0, str(REPO_ROOT))
    from tools.skill_register import SkillRegister

    sr = SkillRegister()
    sr.auto_discover(fast=True)          # fast=True：只解析 SKILL.md，不 import 技能模块
    all_meta = sr.get_all_skills_unchecked()
    enabled = set(sr.get_all_skills().keys())
    tool2skills = {}
    skill2tools = {}
    for name, meta in all_meta.items():
        tools = [t.tool_name for t in meta.tools]
        skill2tools[name] = tools
        for t in tools:
            tool2skills.setdefault(t, set()).add(name)
    src = "SkillRegister().auto_discover(fast=True) @ " + str(REPO_ROOT)
    return tool2skills, skill2tools, sorted(enabled), src


# ─────────────────────────────────────────────────────────────────────
# 2. 逐 run 读 trace
# ─────────────────────────────────────────────────────────────────────
def _load_runs():
    """扫描所有月库，返回 [{db, run_id, agent, created_at, status, msgs, steps}]。"""
    runs = []
    for db in sorted(glob.glob(TRACE_GLOB)):
        conn = _connect_ro(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "select id, agent_name, created_at, status from runs order by created_at"
            ).fetchall()
            for row in rows:
                rid = row["id"]
                msgs = [dict(m) for m in conn.execute(
                    "select m.id, m.seq, m.role, m.content, s.step_type, s.step_name "
                    "from messages m join steps s on m.step_id = s.id "
                    "where s.run_id = ? order by m.step_id, m.seq", (rid,))]
                steps = [dict(s) for s in conn.execute(
                    "select id, step_type, step_name, status, output from steps "
                    "where run_id = ? order by id", (rid,))]
                runs.append({
                    "db": os.path.basename(db), "run_id": rid,
                    "agent": row["agent_name"], "created_at": row["created_at"],
                    "status": row["status"], "msgs": msgs, "steps": steps,
                })
        finally:
            conn.close()
    return runs


def _extract_selected(run):
    """取 (selected_skills, loaded_skills, 来源)。取不到返回 (None, None, 原因)。"""
    for st in run["steps"]:
        if st["step_type"] != "select_skills":
            continue
        try:
            data = json.loads(st["output"] or "{}")
        except (ValueError, TypeError):
            continue
        sel = data.get("selected_skills") or data.get("candidate_skills")
        loaded = data.get("loaded_skills")
        if isinstance(sel, list):
            return ([str(x) for x in sel],
                    [str(x) for x in loaded] if isinstance(loaded, list) else None,
                    "steps.select_skills.output")
    for st in run["steps"]:
        if st["step_type"] != "skill" or st["step_name"] != "select":
            continue
        try:
            data = json.loads(st["output"] or "{}")
        except (ValueError, TypeError):
            continue
        sel = data.get("selected") or data.get("candidates")
        if isinstance(sel, list):
            return [str(x) for x in sel], None, "steps.skill/select.output"
    return None, None, "trace 未落盘 selected_skills"


def _tool_calls(run):
    """实际发生的工具调用（每个 step_type='tool' 的步 = 一次调用），按发生顺序。"""
    return [st["step_name"] for st in run["steps"] if st["step_type"] == "tool"]


def _detail_facts(run):
    """判定该 run 的技能详情消息形态，并抽出「有指南的工具」集合。"""
    tail_tools, tail_skills = set(), set()
    mid_tools, mid_skills = set(), set()
    legacy_surface = False
    for m in run["msgs"]:
        text = m.get("content") or ""
        if LEGACY_SURFACE_RE.search(text):
            legacy_surface = True
        heads = DETAIL_HEADER_RE.findall(text)
        if not heads:
            continue
        tools = set(TOOL_ENTRY_RE.findall(text))
        if text.lstrip().startswith("## 技能"):
            tail_tools |= tools
            tail_skills |= set(heads)
        else:
            mid_tools |= tools
            mid_skills |= set(heads)
    return {"has_tail": bool(tail_skills), "has_mid": bool(mid_skills),
            "legacy_surface": legacy_surface,
            "tail_tools": tail_tools, "tail_skills": tail_skills,
            "mid_tools": mid_tools, "mid_skills": mid_skills}


def _classify(run, sel, facts):
    """返回 (bucket, reason)。bucket ∈ IN_SCOPE / EXCLUDED_OLD / UNDETERMINED。

    顺序有讲究：**先判「无法判定」，再判「旧格式」**。
    - 详情消息整条缺失、或 selected_skills 没落盘 → 无法判定（绝不当作覆盖成功）；
    - 详情存在但形态与现在不同（system_mid 投递 / strict 工具面）→ 旧格式，排除。
    """
    if not facts["has_tail"] and not facts["has_mid"]:
        return "UNDETERMINED", "技能详情消息缺失（trace 里没有「## 技能 … 的工具详情」）"
    if sel is None:
        return "UNDETERMINED", "selected_skills 未捕获"
    if not facts["has_tail"]:
        return "EXCLUDED_OLD", "旧格式：技能详情走 system_mid（拼进主 system 中段），非当前 tail 形态"
    if facts["legacy_surface"]:
        return "EXCLUDED_OLD", "旧格式：strict 工具面（PrepareNode 仍拼「## 可用工具」）"
    return "IN_SCOPE", ""


def _is_wide(run, sel, facts):
    """宽口径：技能详情**内容**形态与现在一致（含 system_mid 投递），且 selected_skills 可捕获。

    只作为敏感性旁证 —— 主口径要求详情是独立 tail system 消息（「该消息存在」）。
    """
    return (sel is not None and not facts["legacy_surface"]
            and (facts["has_tail"] or facts["has_mid"]))


# ─────────────────────────────────────────────────────────────────────
# 3. 主流程
# ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="技能使用指南覆盖率只读探针（不跑 LLM）")
    ap.add_argument("--sample", type=int, default=3, help="人工复核抽样的未覆盖工具数（默认 3）")
    ap.add_argument("--json", default="", help="把结果另存为 JSON（可选）")
    args = ap.parse_args()

    # ---- 映射 ----
    map_b, skill_b, md_files = mapping_from_skill_md()
    map_a, skill_a, enabled_a, src_a = mapping_from_registry()

    print("=" * 78)
    print("映射来源与规模")
    print("=" * 78)
    print("来源 B（SKILL.md「## 工具列表」契约段直解）：技能 %d / 工具 %d（%d 个 SKILL.md）"
          % (len(skill_b), len(map_b), len(md_files)))
    print("来源 A（%s）：技能 %d（启用 %d）/ 工具 %d"
          % (src_a, len(skill_a), len(enabled_a), len(map_a)))
    if not map_a or not map_b:
        print("\n[BLOCKED] 映射为空 —— 不继续计算（避免恒真的 0%）。")
        return 2

    only_a = sorted(set(map_a) - set(map_b))
    only_b = sorted(set(map_b) - set(map_a))
    diffs = {t: (sorted(map_a.get(t, [])), sorted(map_b.get(t, [])))
             for t in set(map_a) | set(map_b) if map_a.get(t) != map_b.get(t)}
    consistent = not only_a and not only_b and not diffs
    print("两来源一致性：%s（仅 A 有 %d / 仅 B 有 %d / 归属集合不同 %d）"
          % ("一致" if consistent else "不一致", len(only_a), len(only_b), len(diffs)))
    if only_a:
        print("  仅 A 有的工具：%s" % only_a)
    if only_b:
        print("  仅 B 有的工具：%s" % only_b)
    for t, (a, b) in sorted(diffs.items()):
        print("  %s: A=%s B=%s" % (t, a, b))

    # ---- 读 run ----
    runs = _load_runs()
    print("\n扫描 %d 个 run（%d 个月库）" % (len(runs), len(set(r["db"] for r in runs))))

    # 来源 C：trace 实测工具面（select_skills 落盘的 all_tool_names）
    surface = set()
    for run in runs:
        for st in run["steps"]:
            if st["step_type"] != "select_skills":
                continue
            try:
                data = json.loads(st["output"] or "{}")
            except (ValueError, TypeError):
                continue
            for t in (data.get("all_tool_names") or []):
                surface.add(str(t))
    unmapped_surface = sorted(surface - set(map_b))
    print("来源 C（trace 实测工具面）：%d 个工具；映射未覆盖的：%s"
          % (len(surface), unmapped_surface if unmapped_surface else "无"))

    # ---- 分类 ----
    in_scope, excluded, undetermined = [], [], []
    for run in runs:
        sel, loaded, sel_src = _extract_selected(run)
        facts = _detail_facts(run)
        bucket, reason = _classify(run, sel, facts)
        run["_sel"], run["_loaded"], run["_sel_src"] = sel, loaded, sel_src
        run["_facts"], run["_bucket"], run["_reason"] = facts, bucket, reason
        run["_wide"] = _is_wide(run, sel, facts)
        {"IN_SCOPE": in_scope, "EXCLUDED_OLD": excluded,
         "UNDETERMINED": undetermined}[bucket].append(run)

    def analyze(run):
        sel = run["_sel"] or []
        loaded = run["_loaded"] if run["_loaded"] is not None else sel
        guides = run["_facts"]["tail_tools"] | run["_facts"]["mid_tools"]
        rows = []
        for tool in _tool_calls(run):
            owners = map_b.get(tool)
            mapped = owners is not None
            rows.append({
                "tool": tool,
                "owners": sorted(owners) if mapped else None,
                "mapped": mapped,
                "covered_sel": bool(mapped and owners & set(sel)),
                "covered_loaded": bool(mapped and owners & set(loaded)),
                "covered_msg": tool in guides,
            })
        return {"calls": rows}

    for run in in_scope + excluded:
        run["_an"] = analyze(run)
    wide_runs = [r for r in in_scope + excluded if r["_wide"]]

    # ---- 逐 run 表 ----
    def print_run_table(title, runs_):
        print("\n" + "=" * 78)
        print(title)
        print("=" * 78)
        for run in runs_:
            calls = run["_an"]["calls"]
            uncovered = [c["tool"] for c in calls if not c["covered_sel"]]
            unc_msg = [c["tool"] for c in calls if not c["covered_msg"]]
            print("\n%s  %-14s %-8s %s" % (run["created_at"][:16], run["agent"],
                                           run["status"], run["run_id"][:8]))
            print("    selected_skills = %s" % run["_sel"])
            print("    loaded_skills   = %s   （来源：%s）" % (run["_loaded"], run["_sel_src"]))
            print("    tools 调用 %d 次 / 去重 %d 个 | 详情消息覆盖 %d/%d | 选中口径未覆盖 %d/%d"
                  % (len(calls), len(set(c["tool"] for c in calls)),
                     len(calls) - len(unc_msg), len(calls), len(uncovered), len(calls)))
            if uncovered:
                cnt = collections.Counter(uncovered)
                print("    未覆盖工具：%s" % dict(cnt))
                for t in sorted(cnt):
                    owners = map_b.get(t)
                    print("      - %s: 所属技能=%s"
                          % (t, sorted(owners) if owners else "【映射外】"))

    print_run_table("纳入统计：%d 个 run（当前形态：tail 投递 + full 工具面 + selected_skills 可捕获）"
                    % len(in_scope), in_scope)

    # ---- 合计 ----
    total_calls = sum(len(r["_an"]["calls"]) for r in in_scope)
    unc_sel = [c for r in in_scope for c in r["_an"]["calls"] if not c["covered_sel"]]
    unc_msg = [c for r in in_scope for c in r["_an"]["calls"] if not c["covered_msg"]]
    unc_loaded = [c for r in in_scope for c in r["_an"]["calls"] if not c["covered_loaded"]]
    unmapped = [c for r in in_scope for c in r["_an"]["calls"] if not c["mapped"]]
    disagree = [c for r in in_scope for c in r["_an"]["calls"]
                if c["mapped"] and c["covered_sel"] != c["covered_msg"]]

    print("\n" + "=" * 78)
    print("合计（仅纳入统计的 run）")
    print("=" * 78)
    print("工具调用总数：%d" % total_calls)
    if total_calls:
        print("未覆盖（选中口径：所属技能 ∉ selected_skills）：%d = %.1f%%"
              % (len(unc_sel), 100.0 * len(unc_sel) / total_calls))
        print("未覆盖（投递口径：所属技能 ∉ loaded_skills）：%d = %.1f%%"
              % (len(unc_loaded), 100.0 * len(unc_loaded) / total_calls))
        print("未覆盖（消息口径：详情消息里没有整行「### <tool>」）：%d = %.1f%%"
              % (len(unc_msg), 100.0 * len(unc_msg) / total_calls))
    print("映射外工具（无法归属技能）：%d%s"
          % (len(unmapped), (" → " + str(sorted(set(c["tool"] for c in unmapped))))
             if unmapped else ""))
    print("选中口径 vs 消息口径 不一致的调用：%d%s"
          % (len(disagree),
             (" → " + str(sorted(set((c["tool"], c["covered_sel"], c["covered_msg"])
                                     for c in disagree)))) if disagree else ""))
    print("\n未覆盖工具 Top（选中口径）：")
    for tool, n in collections.Counter(c["tool"] for c in unc_sel).most_common():
        owners = map_b.get(tool) or []
        print("  %3d 次  %-32s 所属技能：%s"
              % (n, tool, ", ".join(sorted(owners)) if owners else "【映射外】"))

    # ---- 排除集 + 宽口径敏感性（都不计入上面的合计）----
    if excluded:
        ex_calls = sum(len(r["_an"]["calls"]) for r in excluded)
        ex_unc = [c for r in excluded for c in r["_an"]["calls"] if not c["covered_sel"]]
        extra = (" = %.1f%%" % (100.0 * len(ex_unc) / ex_calls)) if ex_calls else ""
        print("\n[排除·旧格式] run %d 个（技能详情存在但形态非当前）：调用 %d 次，未覆盖 %d 次%s"
              % (len(excluded), ex_calls, len(ex_unc), extra))
        for reason, n in collections.Counter(r["_reason"] for r in excluded).most_common():
            print("         %3d 个  %s" % (n, reason))
    wide_calls = sum(len(r["_an"]["calls"]) for r in wide_runs)
    wide_unc = [c for r in wide_runs for c in r["_an"]["calls"] if not c["covered_sel"]]
    wide_pct = ("%.1f%%" % (100.0 * len(wide_unc) / wide_calls)) if wide_calls else "n/a"
    print("[敏感性·宽口径] 若把 system_mid 投递的旧格式 run 也算进来"
          "（详情内容形态一致、只是投递位置不同）：run %d 个，调用 %d 次，未覆盖 %d 次 = %s"
          % (len(wide_runs), wide_calls, len(wide_unc), wide_pct))

    # ---- 无法判定 ----
    print("\n" + "=" * 78)
    print("无法判定：%d 个 run（不计入任何覆盖率分母）" % len(undetermined))
    print("=" * 78)
    for reason, n in collections.Counter(r["_reason"] for r in undetermined).most_common():
        same = [r for r in undetermined if r["_reason"] == reason]
        with_calls = [r for r in same if _tool_calls(r)]
        print("  %3d 个  %s" % (n, reason))
        print("        其中 %d 个 run 实际发生过工具调用（真正影响结论的部分）" % len(with_calls))
        print("        agent 分布：%s" % dict(collections.Counter(r["agent"] for r in same).most_common()))
        for run in with_calls[:6]:
            print("          %s  %-14s %-8s %-8s 工具调用 %d 次"
                  % (run["created_at"][:16], run["agent"], run["status"],
                     run["run_id"][:8], len(_tool_calls(run))))
        if len(with_calls) > 6:
            print("          … 其余 %d 个（有工具调用）" % (len(with_calls) - 6))
    undet_calls = sum(len(_tool_calls(r)) for r in undetermined)
    print("  小计：无法判定 run 里的工具调用 %d 次（一律不判为覆盖成功）" % undet_calls)

    # ---- 抽样人工复核 ----
    print("\n" + "=" * 78)
    print("抽样人工复核（未覆盖工具，取前 %d 个）" % args.sample)
    print("=" * 78)
    top = [t for t, _ in collections.Counter(c["tool"] for c in unc_sel).most_common(args.sample)]
    for tool in top:
        owners = sorted(map_b.get(tool) or [])
        print("\n── 复核工具：%s" % tool)
        print("   两种映射来源给出的所属技能：%s" % owners)
        for run in in_scope:
            calls = [c for c in run["_an"]["calls"] if c["tool"] == tool and not c["covered_sel"]]
            if not calls:
                continue
            sel = run["_sel"] or []
            loaded = run["_loaded"] if run["_loaded"] is not None else sel
            print("   run %s (%s, %s)" % (run["run_id"][:8], run["created_at"][:16], run["agent"]))
            print("     selected_skills=%s" % sel)
            print("     loaded_skills=%s" % loaded)
            print("     所属技能 ∩ selected = %s ；∩ loaded = %s"
                  % (sorted(set(owners) & set(sel)) or "空",
                     sorted(set(owners) & set(loaded)) or "空"))
            heads = sorted(run["_facts"]["tail_skills"])
            print("     详情消息里的技能段（## 技能 X 的工具详情）：%s" % heads)
            print("     其中属于该工具所属技能的：%s"
                  % ([h for h in heads if h in owners] or "无"))
            print("     详情消息里是否有整行「### %s」：%s"
                  % (tool, "有" if tool in run["_facts"]["tail_tools"] else "没有"))
            print("     该工具在本 run 出现次数：%d" % len(calls))
            break

    if args.json:
        payload = {
            "mapping": {"source_b_tools": len(map_b), "source_a_tools": len(map_a),
                        "source_b_skills": len(skill_b), "source_a_skills": len(skill_a),
                        "consistent": consistent, "unmapped_surface": unmapped_surface},
            "in_scope": [{"run_id": r["run_id"], "agent": r["agent"],
                          "created_at": r["created_at"], "selected": r["_sel"],
                          "loaded": r["_loaded"], "calls": r["_an"]["calls"]}
                         for r in in_scope],
            "excluded_old": [{"run_id": r["run_id"], "agent": r["agent"],
                              "created_at": r["created_at"], "reason": r["_reason"]}
                             for r in excluded],
            "undetermined": [{"run_id": r["run_id"], "agent": r["agent"],
                              "created_at": r["created_at"], "reason": r["_reason"]}
                             for r in undetermined],
            "totals": {"calls": total_calls, "uncovered_selected": len(unc_sel),
                       "uncovered_loaded": len(unc_loaded), "uncovered_message": len(unc_msg)},
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("\nJSON 已写出：%s" % args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())

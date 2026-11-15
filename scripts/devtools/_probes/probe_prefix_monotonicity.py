"""前缀单调性探针：校验 ReAct 连续 LLM 调用的 messages 是否「只往尾部追加」。

背景（provider 前缀缓存语义）
---------------------------
provider 的前缀缓存对一次请求的 payload **从第 0 字节开始逐字节比对**，一旦在第 k
条消息处不同，k 之后的内容整段作废、按 miss 价重算。所以「缓存友好」等价于一条
不变量：

    后一次调用的 messages 必须是前一次的**逐字节前缀**（纯尾部追加）

任何中间插入 / 原地改写 / 换序都会让断点之后的前缀失效。本探针只观测这条不变量，
不改任何东西。

它回答三个问题
--------------
1. 相邻两次 LLM 调用的共同前缀有多少条？第一个不同处在第几条？
2. 那一处两侧分别是什么（role / md5 / 字符数 / 首 60 字）？
3. 该次调用实际拿到多少 cached_tokens？丢掉的前缀折多少 token？

判定口径（kind / mode / structural）
----------------------------------
kind（这一对到底是什么关系）：
    append        ✅ 前一次是后一次的逐字节前缀 —— 纯追加，缓存全额命中
    identical     两次 payload 完全相同（重试/幂等）
    tail_replace  ⚠️ 只有**最后一条**不同（同一前缀 + 各自的任务指令，如
                  report-fast「## 现在请撰写分析报告」↔ dashboard「## 本次任务」）
    rewrite       ⚠️ 中途第 k 条被改写（前面 k 条仍可命中）
    truncate      ⚠️ 后一次是前一次的前缀（历史被砍短）
    cold          ⚠️ 共同前缀为 0（两族零字节重叠）

mode（这一对该不该按「续写」算账）：
    chain  两次都是 ReAct 级联调用（node 属 CHAIN_NODES）→ 后一次本应是前一次的扩展
    leaf   至少一侧是「独立指令 + 本次输入」的叶子调用（classify / skill-router /
           react-tool-compress / sentiment-extract 等）→ 两者之间不存在续写关系，
           把 prev 的尾部算成「丢失」是口径错误，故 lost 记 0

structural（能不能靠「改成尾部追加」消除）：
    跨族 / cold / leaf / 跨节点 tail_replace → 结构性，不可硬改，只谈「损失最小化」
    同族 + chain + 同节点 tail_replace / rewrite → **可修**，汇总 B 单列

「prompt 边界」怎么还原
----------------------
TraceRecorder 把**本次请求的 prompt** 与**本次响应**按 seq 递增写进同一个 step 的
messages 表：「core.py::_llm_start」写 prompt（seq 从 0 起），「on_llm_end」接着
写响应（SELECT MAX(seq)+1）。因此：

    prompt 条数 = messages 总条数 − 响应条数
    响应条数    = steps.output 反序列化后的条数（output 为空 ⇒ 失败/未返回 ⇒ prompt 即全部）

探针按这条规则还原，**不猜**；「--debug」会把还原结果（prompt/响应条数）打出来。

只读保证
--------
全部读经「utils.agent_trace.store」门面（与 context_insight_probe.py 同款），
只跑 SELECT；不建表、不写库、**不跑任何 LLM**、不修改 trace。

⚠️ 一个已知口径边界（2026-10-18 在 unified_plan 上实测）
-------------------------------------------------------
message 级「逐字节前缀」是**充分不必要**条件：provider 缓存的是 **token 级**前缀，
而消息骨架的差异（典型是「merge_leading_system」把头部连续 system 合成一条）不一定
构成 token 级差异。实测 unified_plan 的「react-context-summary」头部是**未合并**的
[REACT 1,544][契约 2,054]，agent 侧是**已合并**的 [REACT+契约 3,600] → 本探针判
common=0（cold），但该次调用 cached_tokens=6,656 ≠ 0。
所以探针报的「断点」是**消息级**事实；要不要算成钱，得再看该次调用的 cached_tokens，
两者不一致时**以 cached_tokens 为准**。

用法
----
    python scripts/devtools/probe_prefix_monotonicity.py                  # 最近 3 个 react_stock run
    python scripts/devtools/probe_prefix_monotonicity.py --runs 5
    python scripts/devtools/probe_prefix_monotonicity.py --run 4da96f9f   # run_id 前缀
    python scripts/devtools/probe_prefix_monotonicity.py --agent unified_plan
    python scripts/devtools/probe_prefix_monotonicity.py --json > pairs.json
    python scripts/devtools/probe_prefix_monotonicity.py --table          # 逐 step 片段表
    python scripts/devtools/probe_prefix_monotonicity.py --chain agent    # 只看 agent 家族内部
    python scripts/devtools/probe_prefix_monotonicity.py --table --node planner
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 项目既有口径：3 字符 ≈ 1 token（agents/react/context.py::_estimate_payload_tokens）。
# 探针不装 tokenizer —— 这里只做量级比较与排序，不参与计价。
CHARS_PER_TOKEN = 3
SNIPPET = 60
_NUL = bytes([0])
_NL = chr(10)
_TAB = chr(9)

# 节点 → 家族。同家族内部的连续调用才有「共同前缀」的语义；
# 跨家族（如 classify → select_template）本来就不共享字节，单独归类为 cold。
NODE_FAMILY = {
    "classify": "classify",
    "classifier": "classify",       # unified_plan / plan_solve 的图节点名
    "clarify": "classify",
    "title": "classify",
    "planner": "planner",
    "report_enhance": "report",     # 旧报告链路（plan_solve 时代）
    "unified_executor": "unified",  # Unified Plan 一次性执行器
    "select_template": "select_template",
    "select_skills": "select_skills",
    "skill-selector": "select_skills",
    "skill-router": "skill-router",
    "skill-gap": "skill-router",
    "prepare": "agent",
    "agent": "agent",
    "tools": "tools",
    "react-context-summary": "compaction",
    "react-tool-compress": "tool-compress",
    "report-fast": "report",
    "report-select": "report",
    "report-expand": "report",
    "dashboard": "dashboard",
    "sentiment-extract": "sentiment",
    "partial_summary": "agent",
}

# 节点是否把 tools 数组绑到这次调用（静态事实，读自代码；trace 不存 tools 数组本身，
# 只有 fork 档会在 steps.extra.fork_tools 里留下条数）。仅用于片段表可读性。
TOOLS_EXPECTED = {
    "classify": "—",
    "classifier": "—",
    "planner": "—",
    "clarify": "—",
    "select_skills": "有(fork档)",
    "skill-router": "—",
    "agent": "有",
    "react-context-summary": "有(fork档)",
    "react-tool-compress": "—",
    "report-fast": "有(fork档)",
    "report-select": "有(fork档)",
    "report-expand": "有(fork档)",
    "dashboard": "有(fork档)",
    "sentiment-extract": "—",
}

# 级联节点：payload 由 state 的对话历史累积而来，后一次「应该」是前一次的严格扩展。
# 其余（classify / skill-router / select_skills / react-tool-compress / sentiment-extract）
# 每次都是「独立指令 + 本次输入」，彼此之间不存在续写关系。
CHAIN_NODES = {
    "agent", "report-fast", "report-select", "report-expand",
    "dashboard", "template_report", "partial_summary", "report_enhance",
}

# 片段分类码（--table 用）
CAT_ACROSS = "①跨run稳定"
CAT_INRUN = "②同run内稳定"
CAT_CARRIED = "④递增history"
CAT_VOLATILE = "③每次调用都不同"


# ────────────────────────────────────────────────────────────────
# 数据模型
# ────────────────────────────────────────────────────────────────
def _msg_fp(msg: dict) -> str:
    """一条消息的字节指纹。

    只覆盖**真正进入 payload 的字段**（role / content / tool_calls / tool_call_id）。

    ⚠️ 刻意不含 reasoning：「models.py::_msg_info」把它截断到 2000 字符存储，
    拿截断后的值比对会制造假差异（且 reasoning_content 通常不回填进上下文）。
    """
    h = hashlib.md5()
    for key in ("role", "content", "tool_calls", "tool_call_id"):
        h.update(str(msg.get(key) or "").encode("utf-8", "surrogatepass"))
        h.update(_NUL)
    return h.hexdigest()


def _chars(msg: dict) -> int:
    """一条消息的字符量（content + tool_calls 的 JSON 文本量）。"""
    return len(str(msg.get("content") or "")) + len(str(msg.get("tool_calls") or ""))


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip().replace(_TAB, " ")[:SNIPPET]
    return ""


class Call:
    """一次 LLM 调用的**输入 prompt**（已剔除响应条数）。"""

    __slots__ = ("step_id", "node", "label", "cached", "prompt", "fps", "chars",
                 "n_resp", "resp_unknown", "fork_tools", "model")

    def __init__(self, step: dict):
        extra = _parse_json(step.get("extra"), {}) or {}
        self.step_id = int(step.get("id") or 0)
        self.node = str(extra.get("purpose") or extra.get("node") or step.get("step_name") or "")
        self.label = str(extra.get("label") or "")
        self.model = str(extra.get("model_name") or "")
        tu = extra.get("token_usage") or {}
        self.cached = int(tu.get("cached_tokens") or 0)
        self.fork_tools = extra.get("fork_tools")

        msgs = step.get("messages") or []
        n_resp, resp_unknown = _response_count(step.get("output"), len(msgs))
        self.n_resp = n_resp
        self.resp_unknown = resp_unknown
        self.prompt = msgs[: max(len(msgs) - n_resp, 0)]
        self.fps = [_msg_fp(m) for m in self.prompt]
        self.chars = [_chars(m) for m in self.prompt]
        # trace 侧无法区分 classify 与 clarify：两者 extra 都是 node=classify, label=classify
        # （clarify 的 label 被 graph node 覆盖）。这里按提示词内容补一个标注，
        # ⚠️ 属**推断**：只用于把片段表分开看，不参与任何「违规」判定。
        if self.node in ("classify", "classifier"):
            joined = " ".join(str(m.get("content") or "")[:200] for m in self.prompt[:2])
            if "澄清助手" in joined:
                self.node = "clarify"

    @property
    def display(self) -> str:
        """表格/汇总里的可读节点名（label 与 node 不同且是标识符时带上）。"""
        if self.node == "clarify":       # 推断标记（trace 里与 classify 无法区分）
            return "classify[clarify]"
        lbl = self.label.strip()
        if lbl and lbl != self.node and lbl.replace("-", "").replace("_", "").isascii() and lbl.replace("-", "").replace("_", "").isalnum():
            return f"{self.node}[{lbl}]"
        return self.node

    @property
    def family(self) -> str:
        return NODE_FAMILY.get(self.node, self.node or "(unknown)")

    @property
    def has_tool_marks(self) -> bool:
        """prompt 里是否带着对话级联的痕迹（tool 消息 / 带 tool_calls 的 ai）。"""
        for msg in self.prompt:
            if (msg.get("role") or "") == "tool":
                return True
            if msg.get("tool_calls"):
                return True
        return False

    @property
    def is_chain(self) -> bool:
        """是不是 ReAct 级联里的一次调用。

        **节点判定优先**，不靠 prompt 嗅探：agent 在压缩之后 prompt 里的 tool 消息
        会被摘要替换掉，只看 prompt 会把它误判成叶子调用；反之压缩的 fork 档也含
        tool 消息却只是「借前缀」的一次性调用。
        """
        if self.node == "react-context-summary":
            return self.has_tool_marks          # fork 档借完整对话；standalone 档是叶子
        if self.node not in CHAIN_NODES:
            return False
        # 级联节点也可能发出「一次性子任务」调用（如 template_report 下的证据筛选器
        # 只有 3 条消息、彼此是独立 fork），这类不该按「该续写前一次」算账。
        return self.has_tool_marks or len(self.prompt) >= 4

    def lost_chars(self, from_index: int) -> int:
        return sum(self.chars[from_index:])


def _parse_json(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _response_count(output_raw, n_rows: int) -> tuple:
    """由 steps.output 反推「响应条数」。

    返回 (响应条数, 是否推断不出来)。输出为空 ⇒ 本次没有返回 ⇒ 0 条。
    """
    parsed = _parse_json(output_raw, None)
    if parsed is None:
        return (0, bool(output_raw))
    if isinstance(parsed, list):
        return (len(parsed), False)
    return (0, True)


# ────────────────────────────────────────────────────────────────
# 取数
# ────────────────────────────────────────────────────────────────
def load_calls(run_id: str) -> list:
    """取一个 run 的全部 llm step（按 id 排序）→ 各自的 prompt。"""
    from utils.agent_trace.store import store

    steps = store.get_steps_with_messages(run_id)
    calls = [Call(s) for s in steps if s.get("step_type") == "llm"]
    calls.sort(key=lambda c: c.step_id)
    return calls


def resolve_runs(args) -> list:
    from utils.agent_trace.store import store

    if args.run:
        out = []
        for prefix in [p for p in args.run.split(",") if p.strip()]:
            hits = store.find_run_by_id_prefix(prefix.strip(), limit=5)
            if not hits:
                print(f"未找到 run（前缀）: {prefix}", file=sys.stderr)
            out.extend(hits)
        return out
    return store.list_runs(limit=max(args.runs, 1), agent_name=args.agent or "")


# ────────────────────────────────────────────────────────────────
# 比对
# ────────────────────────────────────────────────────────────────
def compare_pair(prev: Call, cur: Call) -> dict:
    """逐条逐字节比对两份 prompt。"""
    a, b = prev.fps, cur.fps
    common = 0
    for x, y in zip(a, b):
        if x == y:
            common += 1
        else:
            break

    identical = a == b
    if identical:
        verdict, kind = "相同", "identical"
    elif common == len(a) and len(b) > len(a):
        verdict, kind = "✅纯追加", "append"
    elif common == len(b) and len(b) < len(a):
        verdict, kind = "⚠️截断", "truncate"
    elif common == 0:
        verdict, kind = "⚠️整段冷（无共享前缀）", "cold"
    elif common == len(a) - 1 and len(b) >= len(a):
        # 只换了**最后一条**：典型形态是「同一前缀 + 各自的任务指令」
        # （report-fast 的「## 现在请撰写分析报告」↔ dashboard 的「## 本次任务」）。
        # 断点确实存在，但代价仅限那一条指令本身。
        verdict, kind = f"⚠️尾槽替换@第{common}条", "tail_replace"
    else:
        verdict, kind = f"⚠️改写@第{common}条", "rewrite"

    # 丢了什么：prev 从 common 起的全部消息本该命中缓存。
    # ⚠️ common == 0 时不得计损：两族本就零字节重叠（如 classify → select_skills），
    # 把这整段算成「丢失」会让跨族调用霸占优先级榜首，掩盖真正可修的违规。
    mode = "chain" if (prev.is_chain and cur.is_chain) else "leaf"
    # raw  = 断点之后 prev 的全部体量（**字面**的「本该命中却没命中」，用于量级排序）
    # counted = 只有「两次都是级联调用」时才认账。叶子调用（扇出）之间本就不存在续写
    #   关系：prev 的尾部是**另一次**独立任务的输入，算成丢失纯属口径错误。
    lost_chars = prev.lost_chars(common) if common > 0 else 0
    counted_chars = lost_chars if mode == "chain" else 0
    same_family = prev.family == cur.family

    return {
        "prev_step": prev.step_id,
        "cur_step": cur.step_id,
        "prev_node": prev.display,
        "cur_node": cur.display,
        "same_family": same_family,
        "prev_msgs": len(a),
        "cur_msgs": len(b),
        "common": common,
        "index": common,
        "kind": kind,
        "verdict": verdict,
        "appended": (len(b) - len(a)) if kind == "append" else 0,
        "lost_chars": lost_chars,
        "lost_tok_est": lost_chars // CHARS_PER_TOKEN,          # raw，仅用于量级
        "lost_tok_counted": counted_chars // CHARS_PER_TOKEN,   # 认账口径
        "prev_cached": prev.cached,
        "cur_cached": cur.cached,
        "prev_resp_unknown": prev.resp_unknown,
        "cur_resp_unknown": cur.resp_unknown,
        "mode": mode,
        # 结构性 = 不可通过「改成尾部追加」消除的断点。
        # 例外：同一个节点的「尾槽替换」正是可修的那类（如 agent 尾部的去重提示），
        # 只有**跨节点**的尾槽替换（report→dashboard 各写各的任务指令）才是结构性。
        "structural": ((not same_family) or kind == "cold" or mode == "leaf"
                       or (kind == "tail_replace" and prev.node != cur.node)),
        "sides": _diff_sides(prev, cur, common, kind),
    }


def _side(call: Call, index: int) -> dict:
    if index >= len(call.prompt):
        return {"role": "(无)", "md5": "-", "chars": 0, "head": "（本条不存在）"}
    msg = call.prompt[index]
    return {
        "role": msg.get("role") or "?",
        "md5": call.fps[index][:10],
        "chars": call.chars[index],
        "head": _first_line(msg.get("content")),
    }


def _diff_sides(prev: Call, cur: Call, common: int, kind: str) -> list:
    """两侧第一个不同处的细节（append 时给被追加的那条）。"""
    if kind == "append":
        idx = len(prev.prompt)
        return [{"index": idx, "prev": None, "cur": _side(cur, idx)}]
    if kind == "identical":
        return []
    return [{"index": common, "prev": _side(prev, common), "cur": _side(cur, common)}]


# ────────────────────────────────────────────────────────────────
# 输出：逐 run 配对
# ────────────────────────────────────────────────────────────────
def fmt_int(n) -> str:
    return f"{int(n):,}"


def render_run(run: dict, calls: list, chain: str, out: list) -> list:
    pairs = []
    run_id = str(run.get("id") or "")
    out.append("")
    out.append(f"run {run_id}  agent={run.get('agent_name')}  created={str(run.get('created_at'))[:19]}"
               f"  llm_calls={len(calls)}")
    out.append(f"  {'#':>3} {'step':>6}  {'node':<24}{'prompt':>6}{'cached':>8}  {'verdict':<20}common/lost")
    out.append("  " + "-" * 100)
    for i, cur in enumerate(calls, 1):
        if i == 1:
            out.append(f"  {i:>3} {cur.step_id:>6}  {cur.display[:23]:<24}{len(cur.prompt):>6}"
                       f"{fmt_int(cur.cached):>8}  {'（首个调用·无前驱）':<20}")
            continue
        prev = calls[i - 2]
        if chain and chain not in (prev.node, cur.node, prev.display, cur.display,
                                   prev.family, cur.family):
            continue
        p = compare_pair(prev, cur)
        pairs.append(p)
        out.append(f"  {i:>3} {cur.step_id:>6}  {cur.display[:23]:<24}{len(cur.prompt):>6}"
                   f"{fmt_int(cur.cached):>8}  {p['verdict']:<20}"
                   f"{p['common']}/{p['prev_msgs']} 丢≈{fmt_int(p['lost_tok_est'])}tok")
        if p["kind"] in ("rewrite", "cold", "truncate", "tail_replace"):
            for side in p["sides"]:
                idx = side["index"]
                pv, cv = side["prev"], side["cur"]
                out.append(f"        prev[{idx}] {pv['role']:<7} md5={pv['md5']} {fmt_int(pv['chars']):>7}字  {pv['head']}")
                out.append(f"        cur [{idx}] {cv['role']:<7} md5={cv['md5']} {fmt_int(cv['chars']):>7}字  {cv['head']}")
                out.append(f"        ↑ {prev.display} → {cur.display}"
                           f"{'' if p['same_family'] else '（跨族）'}  cached {fmt_int(prev.cached)} → {fmt_int(cur.cached)}")
        if p["prev_resp_unknown"] or p["cur_resp_unknown"]:
            out.append("        ⚠️ output 为空/结构异常：本次 prompt 边界按「无响应行」推断")
    return pairs


# ────────────────────────────────────────────────────────────────
# 输出：汇总（优先级表）
# ────────────────────────────────────────────────────────────────
def render_summary(all_pairs: list, top: int, out: list) -> list:
    """汇总。每个「前 → 后」行同时统计**可修**与**结构性**两组，互不污染。

    可修   = 同族、且两次都是级联调用 —— 理论上后一次就该是前一次的扩展
    结构性 = 跨族 / 叶子扇出 / 跨节点尾槽替换 —— 换成尾部追加也消除不了
    """
    agg = defaultdict(lambda: {"n": 0, "n_fix": 0, "bad_fix": 0, "lost_fix": 0,
                               "n_struct": 0, "lost_all": 0, "kinds_fix": Counter(),
                               "kinds_all": Counter()})
    for p in all_pairs:
        b = agg[(p["prev_node"], p["cur_node"])]
        b["n"] += 1
        b["kinds_all"][p["kind"]] += 1
        broken = p["kind"] not in ("append", "identical")
        if broken:
            b["lost_all"] += p["lost_tok_est"]
        if p["structural"]:
            b["n_struct"] += 1
        else:
            b["n_fix"] += 1
            if broken:
                b["bad_fix"] += 1
                b["lost_fix"] += p["lost_tok_counted"]
                b["kinds_fix"][p["kind"]] += 1

    rows = [{"prev_node": k[0], "cur_node": k[1], **v} for k, v in agg.items()]
    rows.sort(key=lambda r: (-r["lost_all"], -r["bad_fix"]))
    fix_rows = sorted([r for r in rows if r["n_fix"]], key=lambda r: (-r["lost_fix"], -r["bad_fix"]))
    struct_rows = sorted([r for r in rows if r["n_struct"]], key=lambda r: -r["n_struct"])

    out.append("")
    out.append("=" * 106)
    out.append("汇总 A：全部相邻对，按「丢失 token」降序")
    out.append("=" * 106)
    out.append(f"{'前 → 后':<42}{'次数':>5}{'可修违规':>9}{'结构':>5}{'丢≈tok':>10}{'违规率':>7}")
    out.append("-" * 106)
    for r in rows[:top]:
        pair = f"{r['prev_node'][:20]} → {r['cur_node'][:18]}"
        broken = r["bad_fix"] + (r["n_struct"] if r["kinds_all"].get("cold") else 0)
        out.append(f"{pair:<42}{r['n']:>5}{r['bad_fix']:>9}{r['n_struct']:>5}"
                   f"{fmt_int(r['lost_all']):>10}{r['bad_fix'] / r['n'] if r['n'] else 0:>7.0%}")

    total = sum(r["n"] for r in rows)
    lost = sum(r["lost_all"] for r in rows)
    out.append("-" * 106)
    out.append(f"{'合计':<42}{total:>5}{sum(r['bad_fix'] for r in fix_rows):>9}"
               f"{sum(r['n_struct'] for r in struct_rows):>5}{fmt_int(lost):>10}")

    # ── 战报 B：可修项（同族 + 级联）──
    n_fix = sum(r["n_fix"] for r in fix_rows)
    n_bad = sum(r["bad_fix"] for r in fix_rows)
    out.append("")
    out.append("=" * 106)
    out.append(f"汇总 B：可修项（同族 + 两次都是级联调用）共 {n_fix} 对")
    out.append("=" * 106)
    if n_fix:
        out.append(f"  ✅ 纯追加 {n_fix - n_bad} 对 / 共 {n_fix} 对 = "
                   f"{(n_fix - n_bad) / n_fix:.0%}   断点 {n_bad} 处")
        kinds = Counter()
        for r in fix_rows:
            kinds.update(r["kinds_fix"])
        out.append("  断点类型：" + ("  ".join(f"{k}={v}" for k, v in kinds.most_common()) or "-"))
        out.append("")
        out.append(f"  {'前 → 后':<38}{'对':>5}{'断':>5}{'丢≈tok':>10}  断点类型")
        out.append("  " + "-" * 92)
        for r in fix_rows:
            pair = f"{r['prev_node'][:17]} → {r['cur_node'][:18]}"
            kd = ",".join(f"{k}={v}" for k, v in r["kinds_fix"].most_common()) or "-"
            out.append(f"  {pair:<38}{r['n_fix']:>5}{r['bad_fix']:>5}"
                       f"{fmt_int(r['lost_fix']):>10}  {kd}")
    else:
        out.append("  （无）")

    # ── 战报 C：结构性（不可硬改，只能尽量少付）──
    out.append("")
    out.append("=" * 106)
    out.append(f"汇总 C：结构性断点（跨族 / 叶子扇出 / 跨节点尾槽替换）共 "
               f"{sum(r['n_struct'] for r in struct_rows)} 对")
    out.append("=" * 106)
    out.append(f"  {'前 → 后':<38}{'结构':>5}{'断点类型（全部）'}")
    out.append("  " + "-" * 92)
    for r in struct_rows:
        pair = f"{r['prev_node'][:17]} → {r['cur_node'][:18]}"
        kd = ",".join(f"{k}={v}" for k, v in r["kinds_all"].most_common()
                      if k not in ("append", "identical")) or "-"
        out.append(f"  {pair:<38}{r['n_struct']:>5}  {kd}")
    out.append("")
    out.append("注：丢≈tok = 断点之后 prev 的全部体量（字面的「本该命中却没命中」，3 字符/token 估算），")
    out.append("    用于量级排序；汇总 B 只对「同族 + 级联」认账（叶子扇出/跨族的尾部本就不该复用）。")
    return rows


# ────────────────────────────────────────────────────────────────
# 输出：逐 step 片段表
# ────────────────────────────────────────────────────────────────
def classify_fragments(calls_by_run: list) -> dict:
    """给每条消息出现位置打分类码（①跨run稳定 / ②同run内稳定 / ④递增history / ③每次不同）。"""
    runs_of = defaultdict(set)      # (node, idx, fp) -> {run}
    calls_of = Counter()            # (node, run, idx, fp) -> 次数

    for run_id, calls in calls_by_run:
        for c in calls:
            for idx, fp in enumerate(c.fps):
                runs_of[(c.display, idx, fp)].add(run_id)
                calls_of[(c.display, run_id, idx, fp)] += 1

    cats = {}                       # (run, step_id, idx) -> 分类码
    for run_id, calls in calls_by_run:
        for k, c in enumerate(calls):
            nxt = calls[k + 1] if k + 1 < len(calls) else None
            for idx, fp in enumerate(c.fps):
                key = (run_id, c.step_id, idx)
                if len(runs_of[(c.display, idx, fp)]) >= 2:
                    cats[key] = CAT_ACROSS
                elif calls_of[(c.display, run_id, idx, fp)] >= 2:
                    cats[key] = CAT_INRUN
                elif nxt is not None and idx < len(nxt.fps) and nxt.fps[idx] == fp:
                    cats[key] = CAT_CARRIED
                else:
                    cats[key] = CAT_VOLATILE
    return {"cats": cats, "runs_of": runs_of, "calls_of": calls_of}


def render_table(calls_by_run: list, out: list, node_filter: str = "") -> dict:
    stats = classify_fragments(calls_by_run)
    cats = stats["cats"]

    by_node = defaultdict(list)
    for run_id, calls in calls_by_run:
        for c in calls:
            by_node[c.display].append((run_id, c))

    table = {}
    for node in sorted(by_node, key=lambda n: (-len(by_node[n]), n)):
        if node_filter and node_filter not in (node, NODE_FAMILY.get(node, "")):
            continue
        rows = by_node[node]
        per_run = Counter(rid for rid, _ in rows)
        rep_run = per_run.most_common(1)[0][0]
        rep_calls = sorted([c for rid, c in rows if rid == rep_run], key=lambda c: c.step_id)

        cat_counter = Counter(cats[(rid, c.step_id, i)]
                              for rid, c in rows for i in range(len(c.fps)))
        total_msgs = sum(cat_counter.values())
        fork_tools = next((c.fork_tools for _, c in rows if c.fork_tools), None)
        tools_desc = f"有(fork tools={fork_tools})" if fork_tools else TOOLS_EXPECTED.get(node, "?")

        out.append("")
        out.append("=" * 100)
        out.append(f"[{node}]  调用 {len(rows)} 次 / {len(per_run)} 个 run   tools={tools_desc}")
        if total_msgs:
            dist = "  ".join(f"{k}={v}({v / total_msgs:.0%})" for k, v in sorted(cat_counter.items()))
            out.append(f"  片段分类分布：{dist}")
        out.append(f"  代表 run {rep_run}（该节点 {len(rep_calls)} 次调用，逐次列出消息序列）")
        for c in rep_calls:
            out.append(f"   · step {c.step_id} prompt={len(c.prompt)}条 cached={fmt_int(c.cached)}"
                       f"{'' if not c.resp_unknown else '（prompt 边界靠推断）'}")
            for i, msg in enumerate(c.prompt):
                cat = cats[(rep_run, c.step_id, i)]
                out.append(f"       {i:>2}  {(msg.get('role') or '?'):<7}{fmt_int(c.chars[i]):>8}字  "
                           f"{cat:<12} {_first_line(msg.get('content'))}")
        table[node] = {
            "calls": len(rows),
            "runs": len(per_run),
            "tools": tools_desc,
            "cat_dist": dict(cat_counter),
            "rep_run": rep_run,
        }
    return table


# ────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────
def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文/emoji 会炸；统一成 utf-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="前缀单调性探针（只读 trace，不跑 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="最近 N 个 run（默认 3）")
    ap.add_argument("--run", default="", help="指定 run_id（可逗号分隔，支持前缀）")
    ap.add_argument("--agent", default="react_stock", help="runs.agent_name（默认 react_stock）")
    ap.add_argument("--chain", default="", help="只看包含该节点/家族的相邻对（如 agent）")
    ap.add_argument("--table", action="store_true", help="输出逐 step 片段表（替代配对视图）")
    ap.add_argument("--node", default="", help="--table 时只显示该节点")
    ap.add_argument("--json", action="store_true", help="结构化输出（JSON）")
    ap.add_argument("--top", type=int, default=25, help="汇总表最多显示多少行")
    ap.add_argument("--debug", action="store_true", help="打印 prompt/响应条数还原细节")
    return ap


def main(argv=None) -> int:
    _ensure_utf8_stdout()
    args = build_parser().parse_args(argv)

    runs = resolve_runs(args)
    if not runs:
        print(f"没有匹配的 run（agent={args.agent!r}）", file=sys.stderr)
        return 2

    calls_by_run = []
    for run in runs:
        rid = str(run.get("id") or "")
        calls = load_calls(rid)
        if calls:
            calls_by_run.append((rid, calls))

    if args.debug:
        for rid, calls in calls_by_run:
            for c in calls:
                print(f"[debug] run={rid[:8]} step={c.step_id} node={c.node} "
                      f"prompt={len(c.prompt)} n_resp={c.n_resp} unknown={c.resp_unknown}",
                      file=sys.stderr)

    payload = {"runs": [], "pairs": [], "summary": [], "table": {}}

    if args.table:
        out = []
        payload["table"] = render_table(calls_by_run, out, node_filter=args.node)
        print(_NL.join(out))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    out = []
    all_pairs = []
    for run, (rid, calls) in zip(runs, calls_by_run):
        pairs = render_run(run, calls, args.chain, out)
        all_pairs.extend(pairs)
        payload["runs"].append({"run_id": rid, "agent": run.get("agent_name"),
                                "llm_calls": len(calls), "pairs": len(pairs)})
    payload["pairs"] = all_pairs
    payload["summary"] = render_summary(all_pairs, args.top, out)

    print(_NL.join(out))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

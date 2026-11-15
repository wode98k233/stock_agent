# -*- coding: utf-8 -*-
"""澄清反问（clarify）精简块路径的真机验证 —— 只读 + 单次真实 run。

背景（已实测，本脚本用来锁死）：
  · classify / clarify / planner 的共享块已换成**精简块**（去掉 [相关记忆片段]，
    见 agents/base.py::register_lean_input / get_lean_input，commit 6d7cae2）；
  · clarify 在 CLI/TUI/直跑时**永不触发** —— agents/shared/clarify.py:80-86 的门控是
    budget_decision_ctx 非 None（只由 Web 运行时安装）。这不是 bug，是设计门控。
    所以本脚本**在进程内安装假回调**（不起 HTTP/SSE 栈）来打开这道门；
    回调收到 clarify_required 后按每题 recommended 作答并**立即返回**（不等 30s）。

四条验证（本脚本的全部价值）：
  ① 触发     —— clarify 的 LLM 调用确实发生（trace 里含「澄清助手」的 LLM step，
                外加 step_type=clarify 的「澄清反问」step）
  ② 精简块   —— 该 step 的 messages[0] 不含 [相关记忆片段]、量级远小于完整块，
                且与 classify 的 messages[0] **逐字节相同**
                （CODE_WIKI 记的精简块基准约 258 字符；本环境画像/持仓块较大时会更大，
                 脚本对此打对照而不硬卡窗口）
  ③ 回答注入 —— 假回调的回答被拼回后续输入（enriched_input 含【用户补充信息（澄清回答）】）
  ④ 拼回孪生 —— clarify 拼回答时**同时登记精简孪生**（clarify.py:156-161）：
                planner 的输入必须是「精简块 + 澄清回答」（含回答、不含记忆）。
                漏登记时 get_lean_input 查不到键 —— **原样返回完整块（带记忆）**，
                所以这里用「注册表实测」+「planner 实际 payload」两路互证，而不是只看代码。

用法（解释器：venv/Scripts/python.exe）：
    python scripts/devtools/verify_clarify_lean_input.py           # 预演（不花钱）
    python scripts/devtools/verify_clarify_lean_input.py --run     # 真跑一次（真实 LLM）
    python scripts/devtools/verify_clarify_lean_input.py --run --fallback "帮我分析一只股票"

警告：带 --run 会真实调用 LLM 跑完一整轮分析（分类 + 澄清 + 规划 + 执行 + 报告）。
   --fallback 仅在「首选问题没触发澄清」时才会再跑一轮（每个候选都是一整轮真实 run）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
import time
import traceback
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 稳定触发澄清的模糊问题（test/e2e/test_cancel_clarify_e2e.py 实测：5 个候选均返回 3 问）
DEFAULT_QUERY = "帮我配置一下仓位"
DEFAULT_MODE = "unified_plan"

MEM_TAG = "[相关记忆片段]"
CLARIFY_MARK = "你是选股雷达的澄清助手"                  # CLARIFY_PROMPT 首句
CLASSIFY_MARK = "判断用户问题是否属于股票/金融分析范畴"    # CLASSIFIER_PROMPT 首句
PLANNER_MARK = "选股雷达的规划器"                        # plan_step 的静态规划指令
ANSWER_HEADER = "【用户补充信息（澄清回答）】"

# 进程内观测槽（全部由探针写入；run 期间 stdio 会被重定向到日志，故统一在结束后打印）
REC: dict = {
    "clarify_payloads": [],   # 假回调收到的 clarify_required payload
    "clarify_result": None,   # maybe_clarify 的返回值（含 enriched_input）
    "lean_calls": [],         # get_lean_input 的每次调用（含调用方）
    "memory_fragments_len": None,
    "planner_payload": None,  # plan_step 实际送给 LLM 的 messages 文本
    "registry_size": None,    # run context 内读到的精简注册表条数
    "twin_registered": None,  # clarify 登记的那条孪生（在 run context 内读出）
}


# ===============================================================
# 探针：假 Web 决策通道 + 三处观测点（只读，不改生产代码）
# ===============================================================

def _install_probes() -> None:
    import agents.base as base_mod
    import agents.shared.clarify as clarify_mod
    import agents.plan.node.planner as planner_mod

    # --- 观测点 1：get_lean_input 每次调用（谁调的 / 进出字符数 / 是否含记忆）---
    real_gli = base_mod.get_lean_input

    def spy_gli(full):
        out = real_gli(full)
        try:
            f = sys._getframe(1)
            caller = f"{Path(f.f_code.co_filename).name}::{f.f_code.co_name}"
        except Exception:
            caller = "?"
        REC["lean_calls"].append({
            "caller": caller,
            "in_len": len(full or ""),
            "in_has_mem": MEM_TAG in (full or ""),
            "out_len": len(out or ""),
            "out_has_mem": MEM_TAG in (out or ""),
            "out_has_answer": ANSWER_HEADER in (out or ""),
        })
        return out

    base_mod.get_lean_input = spy_gli

    # --- 观测点 2：maybe_clarify 的返回值（③ 的 enriched_input 直接来源）---
    real_mc = clarify_mod.maybe_clarify

    async def spy_mc(*a, **kw):
        res = await real_mc(*a, **kw)
        REC["clarify_result"] = res
        # 顺带在 run **自己的** context 里读一眼精简注册表：这里读到的就是 planner
        # 稍后会查到的那张表。绝不能在 run 之外读 —— asyncio.run 会拷贝 context，
        # run 内 set 的 ContextVar 不回流到调用方，外层读到的是空表（探针假象，
        # 会被误判成「漏登记」）。
        try:
            from agents.base import _lean_registry
            reg = _lean_registry.get() or {}
            ri = (res or {}).get("enriched_input") or ""
            REC["registry_size"] = len(reg)
            REC["twin_registered"] = reg.get(ri)
        except Exception:
            pass
        return res

    clarify_mod.maybe_clarify = spy_mc

    # --- 观测点 3：plan_step 实际发给 LLM 的 messages（④ 的最终证据）---
    real_pj = planner_mod.llm_json_with_retry

    def spy_pj(llm, messages, logger, *a, **kw):
        try:
            REC["planner_payload"] = [str(getattr(m, "content", m)) for m in messages]
        except Exception:
            REC["planner_payload"] = []
        return real_pj(llm, messages, logger, *a, **kw)

    planner_mod.llm_json_with_retry = spy_pj


def _make_answer_cb():
    """假的 budget_decision_ctx 回调：按每题 recommended 作答并立即返回（不睡 30s）。"""
    def cb(payload):
        REC["clarify_payloads"].append(payload)
        answers = {q["id"]: q.get("recommended") for q in (payload.get("questions") or [])}
        return json.dumps({"answers": answers}, ensure_ascii=False)
    return cb


def _progress_cb(event):
    """抓「记忆上下文」进度事件：证明完整块里确实有记忆（② 才不是空对空）。"""
    try:
        data = getattr(event, "data", None) or {}
        if "memory_fragments" in data:
            REC["memory_fragments_len"] = len(data.get("memory_fragments") or "")
    except Exception:
        pass


# ===============================================================
# 单次真实 run（进程内，与 scripts/devtools/verify_e2e_modes.py 同一条路径）
# ===============================================================

def _run_once(mode: str, query: str, timeout: int) -> dict:
    from utils.bootstrap_common import init_common
    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from agents import register_all
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry
    from agents.shared.budget_ctx import budget_decision_ctx
    from agents.shared.raw_input_ctx import reset_raw_input, set_raw_input

    common = init_common()
    logger, dialog_uuid, _ctx, log_file = get_logger(query)
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

    _install_probes()

    print(f"[prep] mode={mode}")
    print(f"[prep] query={query}")
    print(f"[prep] log={log_file}")

    async def _main():
        token = budget_decision_ctx.set(_make_answer_cb())
        raw_tok = set_raw_input(query)
        t0 = time.time()
        err = None
        resp = None
        try:
            resp = await asyncio.wait_for(
                agent.run(query, registry, memory, logger, progress_callback=_progress_cb),
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001 —— 验证脚本要如实报告失败
            err = e
        finally:
            reset_raw_input(raw_tok)
            budget_decision_ctx.reset(token)
        return resp, time.time() - t0, err

    resp, dur, err = asyncio.run(_main())
    text = getattr(resp, "response", None) or getattr(resp, "content", None) or str(resp or "")
    print(f"[run] 用时 {dur:.0f}s，回答 {len(text)} 字，异常={err!r}")
    if err is not None:
        traceback.print_exception(type(err), err, err.__traceback__)
    return {"mode": mode, "query": query, "seconds": dur, "log_file": log_file,
            "response": text, "error": err}


# ===============================================================
# trace 侧取证
# ===============================================================

def _find_run(mode: str, query: str):
    from utils.agent_trace.store import store
    for r in store.list_runs(limit=30, agent_name=mode):
        if query in (r.get("input") or ""):
            return r
    return None


def _texts(step: dict) -> list:
    return [(m.get("content") or "") for m in (step.get("messages") or [])]


def _has_mark(step: dict, mark: str) -> bool:
    return any(mark in t for t in _texts(step))


def _msg0(step: dict) -> str:
    msgs = step.get("messages") or []
    return (msgs[0].get("content") or "") if msgs else ""


def _strip_ansi(s: str) -> str:
    """去掉终端颜色码（避免在 Python 源码里写转义序列）。"""
    out, skip = [], False
    for ch in s:
        if ch == chr(27):
            skip = True
            continue
        if skip:
            if ch == "m":
                skip = False
            continue
        out.append(ch)
    return "".join(out)


def _read_summary_lines(log_file) -> list:
    if not log_file or not Path(log_file).exists():
        return []
    keys = ("请求完成汇总", "缓存统计", "缓存明细", "模型明细",
            "供应商明细", "预算使用", "统计校验", "成本")
    out = []
    for raw in Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines():
        line = _strip_ansi(raw).rstrip()
        if any(k in line for k in keys):
            out.append(line)
    return out


# ===============================================================
# 四条判据
# ===============================================================

def _expected_answer_labels() -> list:
    """假回调的作答经 resolve_answer_text 解析后、应当注入的中文文案列表。"""
    from agents.shared.clarify import resolve_answer_text
    lines = []
    for payload in REC["clarify_payloads"]:
        for q in payload.get("questions") or []:
            a = resolve_answer_text(q, q.get("recommended"))
            if a:
                lines.append(f"- {q['question']}：{a}")
    return lines


def _verify(run: dict, steps: list):
    checks = []

    def chk(tag, ok, detail):
        checks.append((tag, bool(ok), detail))
        flag = "OK " if ok else "FAIL"
        print(f"  [{flag}] {tag}：{detail}")

    def banner(title):
        print()
        print("=" * 96)
        print(title)
        print("=" * 96)

    llm_steps = [s for s in steps if s.get("step_type") == "llm"]
    clarify_llm = next((s for s in llm_steps if _has_mark(s, CLARIFY_MARK)), None)
    classify_llm = next((s for s in llm_steps if _has_mark(s, CLASSIFY_MARK)), None)
    planner_llm = next((s for s in llm_steps if _has_mark(s, PLANNER_MARK)), None)
    clarify_meta = next((s for s in steps if s.get("step_type") == "clarify"), None)

    banner("① 触发：clarify 的 LLM 调用确实发生")
    c_meta_extra = {}
    if clarify_meta:
        try:
            c_meta_extra = json.loads(clarify_meta.get("extra") or "{}")
        except Exception:
            c_meta_extra = {}
    chk("①-a trace 有含「澄清助手」的 LLM step",
        clarify_llm is not None,
        f"step_name={(clarify_llm or {}).get('step_name')!r} step_type=llm "
        f"msgs={len((clarify_llm or {}).get('messages') or [])}")
    chk("①-b trace 有 step_type=clarify 的「澄清反问」step",
        clarify_meta is not None,
        f"step_name={(clarify_meta or {}).get('step_name')!r} "
        f"extra={json.dumps(c_meta_extra, ensure_ascii=False)}")
    chk("①-c 假回调确实收到 clarify_required 并作答",
        bool(REC["clarify_payloads"]),
        f"收到 {len(REC['clarify_payloads'])} 次 · "
        f"event_name={(REC['clarify_payloads'][0].get('event_name') if REC['clarify_payloads'] else None)!r} · "
        f"questions={(len(REC['clarify_payloads'][0].get('questions') or []) if REC['clarify_payloads'] else 0)}")

    banner("② 精简块：messages[0] 无 [相关记忆片段]、量级远小于完整块、与 classify 逐字节相同")
    m0_clarify = _msg0(clarify_llm) if clarify_llm else ""
    m0_classify = _msg0(classify_llm) if classify_llm else ""
    chk("②-a 完整块确实带记忆（否则②是空对空）",
        (REC["memory_fragments_len"] or 0) > 0
        or any(c["in_has_mem"] for c in REC["lean_calls"]),
        f"progress.memory_fragments={REC['memory_fragments_len']} 字符 · "
        f"lean 调用中入参含记忆的次数={sum(1 for c in REC['lean_calls'] if c['in_has_mem'])}")
    chk("②-b clarify messages[0] 不含 [相关记忆片段]",
        clarify_llm is not None and MEM_TAG not in m0_clarify,
        f"len={len(m0_clarify)} · HAS_MEM={MEM_TAG in m0_clarify} · 首 60 字={m0_clarify[:60]!r}")
    # 口径说明：CODE_WIKI 记的精简块基准约 258 字符，那是在「画像/持仓块为空」的环境测的。
    # 本仓库当前环境会注入 时间 + 市场状态 + 用户画像 + 持仓账本，精简块会大一截；② 真正要
    # 证明的是「记忆段被摘掉、且与 classify 同族」——判据落在量级对照上，并对 250~400 打对照。
    _segs = [t for t in ("[背景信息]", "[用户问题]", "[市场状态]", "[相关记忆片段]",
                         "## 用户画像", "[用户持仓账本]") if t in m0_clarify]
    print(f"     · 精简块含有的段落标记：{_segs}")
    in_range = 250 <= len(m0_clarify) <= 400
    chk("②-c 精简块远小于完整块（记忆段已摘掉）",
        100 <= len(m0_clarify) <= 1500,
        f"lean_len={len(m0_clarify)} · " + ("落在 CODE_WIKI 基准 250~400 内"
                                            if in_range else "大于 CODE_WIKI 基准 250~400（本环境画像/持仓块较大，见报告）"))
    chk("②-d 与 classify 的 messages[0] 逐字节相同",
        bool(m0_clarify) and m0_clarify == m0_classify,
        f"clarify_len={len(m0_clarify)} · classify_len={len(m0_classify)} · "
        f"identical={m0_clarify == m0_classify} · "
        f"md5_clarify={hashlib.md5(m0_clarify.encode('utf-8')).hexdigest()} · "
        f"md5_classify={hashlib.md5(m0_classify.encode('utf-8')).hexdigest()}")

    banner("③ 回答注入：假回调的回答被拼回后续输入（enriched_input 含澄清回答）")
    res = REC["clarify_result"] or {}
    enriched = res.get("enriched_input") or ""
    expected = _expected_answer_labels()
    chk("③-a maybe_clarify 返回了 enriched_input",
        bool(enriched),
        f"len={len(enriched)} · answers={json.dumps(res.get('answers'), ensure_ascii=False)[:200]}")
    chk("③-b enriched_input 含澄清回答块与每条回答",
        ANSWER_HEADER in enriched and all(line in enriched for line in expected),
        f"HAS_HEADER={ANSWER_HEADER in enriched} · 应有 {len(expected)} 条回答、命中 "
        f"{sum(1 for line in expected if line in enriched)} 条")
    chk("③-c enriched_input 保留完整块（含记忆，因为它是喂主链的）",
        MEM_TAG in enriched,
        f"HAS_MEM={MEM_TAG in enriched}（应为 True：拼回的是完整块）")

    banner("④ 拼回后的精简孪生：planner 输入 = 精简块 + 澄清回答（含回答、不含记忆）")
    # ⚠️ 这里**不能**从当前（外层）context 读 _lean_registry：asyncio.run 会拷贝 context，
    # run 内 set() 的 ContextVar 不回流到调用方 —— 外层读到的是空表，那是探针假象而不是
    # 「漏登记」。孪生由 spy_mc 在 run 自己的 context 里读出（REC["twin_registered"]）。
    nl = chr(10)
    twin = REC.get("twin_registered")
    answers_block = "" if not expected else nl + nl + ANSWER_HEADER + nl + nl.join(expected)
    chk("④-a run context 内读注册表：enriched_input 有精简孪生",
        twin is not None,
        f"注册表 {REC.get('registry_size')} 条 · twin_len={len(twin or '')} · "
        f"twin_HAS_MEM={MEM_TAG in (twin or '')} · twin_HAS_ANSWER={ANSWER_HEADER in (twin or '')}")
    chk("④-b 孪生 == 精简块 + 澄清回答（拼接式恒等，且无记忆）",
        bool(twin) and twin == (m0_clarify + answers_block) and MEM_TAG not in twin,
        f"twin_len={len(twin or '')} · msg0_len={len(m0_clarify)} · 回答块_len={len(answers_block)} · "
        f"恒等={twin == (m0_clarify + answers_block)} · HAS_MEM={MEM_TAG in (twin or '')}")
    planner_texts = _texts(planner_llm) if planner_llm else []
    planner_hit = [t for t in planner_texts if ANSWER_HEADER in t]
    chk("④-c 孪生 == planner 实际收到的输入（端到端恒等）",
        bool(twin) and bool(planner_hit) and twin == planner_hit[0],
        f"twin_len={len(twin or '')} · planner_len={len(planner_hit[0]) if planner_hit else 0} · "
        f"identical={bool(twin) and bool(planner_hit) and twin == planner_hit[0]}")
    chk("④-d planner 的 LLM step 里确实出现「精简块 + 澄清回答」",
        bool(planner_hit) and all(MEM_TAG not in t for t in planner_hit),
        f"planner step msgs={len(planner_texts)} · 含回答的消息={len(planner_hit)} · "
        f"该消息 len={len(planner_hit[0]) if planner_hit else 0} · "
        f"HAS_MEM={any(MEM_TAG in t for t in planner_hit)}")
    payload = REC["planner_payload"] or []
    payload_hit = [t for t in payload if ANSWER_HEADER in t]
    chk("④-e plan_step 探针：实际发给 LLM 的 messages 含回答且不含记忆",
        bool(payload_hit) and all(MEM_TAG not in t for t in payload_hit),
        f"payload msgs={len(payload)} · 含回答={len(payload_hit)} · "
        f"HAS_MEM={any(MEM_TAG in t for t in payload_hit)}")
    planner_calls = [c for c in REC["lean_calls"] if c["caller"].endswith("planner.py::plan_step")]
    chk("④-f 反证：planner 传入 get_lean_input 的是带记忆完整块，取回的是无记忆精简块",
        any(c["in_has_mem"] and not c["out_has_mem"] for c in planner_calls),
        "; ".join(f"{c['caller']} in={c['in_len']}(mem={c['in_has_mem']}) "
                  f"out={c['out_len']}(mem={c['out_has_mem']},ans={c['out_has_answer']})"
                  for c in planner_calls) or "（未捕获到 planner 的 get_lean_input 调用）")

    banner("附：同一次 run 的 get_lean_input 全量调用轨迹（调用方 / 进出字符数 / 是否含记忆）")
    for c in REC["lean_calls"]:
        print(f"  {c['caller']:<34} in={c['in_len']:>6}(mem={str(c['in_has_mem']):<5}) "
              f"out={c['out_len']:>6}(mem={str(c['out_has_mem']):<5}, ans={str(c['out_has_answer']):<5})")

    info = {
        "run_id": (run or {}).get("id"),
        "clarify_msg0_len": len(m0_clarify),
        "classify_msg0_len": len(m0_classify),
        "enriched_len": len(enriched),
        "twin_len": len(twin or ""),
        "planner_input_len": len(payload_hit[0]) if payload_hit else 0,
        "expected_answers": expected,
    }
    return checks, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="真跑（不带时只预演，不花钱）")
    ap.add_argument("--query", default=DEFAULT_QUERY, help="信息不足的问题（默认已实测可触发澄清）")
    ap.add_argument("--fallback", action="append", default=[],
                    help="首选问题没触发澄清时的备用问题（可多次；每个候选都是一整轮真实 run）")
    ap.add_argument("--mode", default=DEFAULT_MODE, help="agent 模式（默认 unified_plan，走 plan_step）")
    ap.add_argument("--timeout", type=int, default=1500, help="单轮 run 的墙钟上限（秒）")
    args = ap.parse_args()

    candidates = [args.query] + list(args.fallback)
    print(f"[plan] 候选问题 {len(candidates)} 个：{candidates}")
    print(f"[plan] 模式={args.mode}（planner = agents/plan/node/planner.py::plan_step）")
    print("[plan] 假回调：收到 clarify_required 即按每题 recommended 作答并立即返回（不睡 30s，不起 HTTP/SSE）")

    if not args.run:
        print()
        print("[dry-run] 未真跑。加 --run 执行（会真实调用 LLM 一整轮）。")
        return 0

    last = None
    for i, q in enumerate(candidates, 1):
        REC["clarify_payloads"].clear()
        REC["clarify_result"] = None
        REC["lean_calls"].clear()
        REC["planner_payload"] = None
        REC["memory_fragments_len"] = None
        print()
        print("#" * 96)
        print(f"# 候选 {i}/{len(candidates)}：{q}")
        print("#" * 96)
        run = _run_once(args.mode, q, args.timeout)
        last = (q, run)
        if REC["clarify_payloads"]:
            break
        print(f"「{q}」未触发澄清；"
              f"{'继续下一个候选' if i < len(candidates) else '无更多候选'}")

    query, run = last
    print(f"[final] 最终使用的问题：{query}")

    tr = _find_run(args.mode, query)
    steps = []
    if tr:
        from utils.agent_trace.store import store
        steps = store.get_steps_with_messages(tr["id"])
    print(f"[trace] run_id={(tr or {}).get('id')} steps={len(steps)}")
    for s in steps:
        print(f"   - {str(s.get('step_type')):<12} {str(s.get('step_name')):<28} "
              f"msgs={len(s.get('messages') or [])}")

    print()
    print("=" * 96)
    print("run 汇总行（来自对话日志）")
    print("=" * 96)
    for line in _read_summary_lines(run["log_file"]):
        print(line)

    checks, info = _verify(tr, steps)
    ok = all(c[1] for c in checks)

    print()
    print("=" * 96)
    print("结论")
    print("=" * 96)
    print(json.dumps({"query": query, "mode": args.mode, **info}, ensure_ascii=False, indent=2))
    print()
    print("四条全部通过" if ok else "存在未通过项：" + ", ".join(c[0] for c in checks if not c[1]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

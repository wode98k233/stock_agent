"""前缀排布对照探针：L0 / L1 / L2 三种「step 间消息槽」布局的真实缓存命中。

回答的问题
----------
用户提案是「每个 step 各带一份背景 + 自己的 system，问题放 user」。本探针在同一
``x-opencode-session`` 下真实发请求，量出三种布局的 ``cached_tokens``：

    L0（现状）      [system 背景][user 步骤A指令]   →  下一步 [system 背景][user 步骤B指令]
    L1（用户提案）  [system 背景][system 步骤A指令][user 具体问题A]
                                                   →  下一步只换第 2、3 条
    L2（追加式）    [system 背景][system 通用规则][user 步骤A指令]
                                                   →  append [assistant A输出][user 步骤B指令]
                                                   →  append [assistant B输出][user 步骤C指令]

判定口径
--------
provider 的 prompt cache 是「从 payload 第 0 字节起的连续前缀」字节匹配。因此：

* 只有**与上一次请求逐字节相同的前缀**才会被计入 ``cached_tokens``；从第一个
  不同处之后整段按 miss 计。
* 为了把「共享一段前缀」与「上一次请求的完整续写」分开，另发两组对照：
  C1 完全相同重发（证明缓存可命中）、C2 纯追加（证明命中来自续写）。

⚠️ **必须等 ≥10 秒**：provider 的缓存写入到可读之间有秒级延迟（实测同 run 内
1~3 秒后的调用仍然 0 命中）。所以每次「写」之后默认 sleep 15 秒再发「读」，
``--gap-seconds`` 可调；把 gap 设成 0 得到的 0 命中**不能**用来评价布局。

⚠️ **payload 太小也不进缓存**：实测输入 <4,000 token 时，即使完全相同重发也可能
0 命中。本探针把单次输入做到 ≈7,000 token（背景 5.3k 字符 + 步骤指令 5.6k 字符），
避免把「未达缓存门槛」误读成「布局无效」。

只读保证
--------
只发 ``max_tokens=8`` 的极小补全（11 次），不写业务库、不改任何代码路径。
``--dry-run`` 完全不联网，只打印将发送的消息槽与本地前缀 sha。

用法
----
    python scripts/devtools/probe_prefix_layout.py --dry-run
    python scripts/devtools/probe_prefix_layout.py                  # 真实调用，默认 15s gap
    python scripts/devtools/probe_prefix_layout.py --gap-seconds 20
    python scripts/devtools/probe_prefix_layout.py --json           # 追加机器可读 JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 项目既有口径：3 字符 ≈ 1 token（agents/react/context.py::_estimate_payload_tokens）
CHARS_PER_TOKEN = 3

SYSTEM_RULES = (
    "【通用分析规则】\n"
    "1. 先给结论，再给证据；证据必须注明数据来源与口径。\n"
    "2. 不预测点位，不承诺收益；风险提示前置。\n"
    "3. 数值保留两位小数，百分比保留一位小数，日期用 YYYY-MM-DD。\n"
    "4. 缺失数据必须写「数据缺失」，不得用常识补全。\n"
    "5. 同一结论不得在两个板块重复展开。\n"
    "6. 引用工具结果时保留原始单位（元/万元/亿元）。\n"
    "7. 涉及个股必须同时给出行业与市值分位。\n"
    "8. 涉及指数必须说明是全收益还是价格指数。\n"
)

QUESTION_A = "当前要分析的问题 A：某只主板股票的近 20 日量价关系。"
QUESTION_B = "当前要分析的问题 B：同一只股票的近 60 日资金流向。"
QUESTION_C = "当前要分析的问题 C：同一只股票的估值分位与行业对比。"

# 真实形态：共享背景 3,335~4,921 字符，各 step 自己的技能指令 7,922~10,232 字符。
# 探针按同量级取值（背景 ~5k 字符、步骤指令 ~5k 字符），否则「背景占 99%」会让
# L0/L1 看起来也有 98% 命中，掩盖「第二步起分叉」的事实。
BG_LINES = 30
STEP_LINES = 30


def build_step(tag: str, reply: str, lines: int = STEP_LINES) -> str:
    """构造一段体量与真实技能指令同量级的「本步骤专属指令」。"""
    head = f"【本步骤专属指令 {tag}】只对当前步骤生效，下一步会整段替换，不得复用。\n"
    body = []
    for i in range(1, lines + 1):
        body.append(
            f"{i:02d}. 本步骤要求：先确认数据口径与时间窗口，再按「结论-证据-风险」三段展开；"
            "涉及个股必须给出行业与市值分位，涉及指数必须说明价格指数还是全收益指数；"
            "所有数值保留两位小数，百分比保留一位小数，日期统一 YYYY-MM-DD；"
            "缺失数据写「数据缺失」，不得用常识补全；同一结论不得重复展开；"
            "证据必须注明来源工具与查询参数，便于复核；"
            "本条为步骤专属要求，仅本次调用有效。"
        )
    return head + "\n".join(body) + f"\n完成以上要求后，请只回复「{reply}」，不要解释。"


STEP_A_FULL = build_step("A", "A-OK")
STEP_B_FULL = build_step("B", "B-OK")
STEP_C_FULL = build_step("C", "C-OK")


def build_background(salt: str, lines: int = BG_LINES) -> str:
    """构造逐字节确定的共享背景（≈5k 字符，与真实共享块同量级）。

    salt 让不同布局的背景在第 0 字节就不同，避免布局之间互相「蹭」缓存，
    否则 L1 的读会命中 L0 写热的同一段背景，量出来的数字无法归因。
    """
    head = f"【背景资料 {salt}】以下为 A 股研究通用口径，供本次多步骤分析逐字复用。\n"
    body = []
    for i in range(1, lines + 1):
        body.append(
            f"{i:03d}. 固定口径第 {i} 条：交易时段为 09:30-11:30 与 13:00-15:00；"
            "涨跌幅限制为主板±10%、创业板与科创板±20%、ST±5%、北交所±30%；"
            "成交额按全市场合计口径，换手率按流通股本口径，资金流按主力净额口径；"
            "复权方式默认前复权，财务数据按报告期口径，估值分位按近五年口径；"
            "本条为背景资料，不得改写、不得摘要、不得省略。"
        )
    return head + "\n".join(body)


def canonical(role: str, content: str) -> str:
    """消息的规范化字节视图（与 provider 的序列化字段一一对应）。"""
    return json.dumps({"role": role, "content": content},
                      ensure_ascii=False, separators=(",", ":"))


def messages_digest(messages: list[tuple[str, str]]) -> tuple[str, int, int]:
    """返回 (前缀 sha 前 16 位, 字符数, token 估算)。"""
    raw = "\x01".join(canonical(r, c) for r, c in messages)
    return (hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            len(raw), int(len(raw) / CHARS_PER_TOKEN))


def common_prefix_chars(a: list[tuple[str, str]], b: list[tuple[str, str]]) -> int:
    """两条 payload 的公共前缀字符数（逐字节比较，用于证明「从第几条起分叉」）。"""
    ra = "\x01".join(canonical(r, c) for r, c in a)
    rb = "\x01".join(canonical(r, c) for r, c in b)
    n = min(len(ra), len(rb))
    i = 0
    while i < n and ra[i] == rb[i]:
        i += 1
    return i


def _slot_label(messages: list[tuple[str, str]]) -> str:
    return "".join(f"[{role}:{len(content)}字]" for role, content in messages)


def _usage(resp: Any) -> dict:
    """从 AIMessage 取 usage（与 utils/llm_factory.py::_extract_llm_usage 同口径）。

    ⚠️ 不能用 ``utils.token_usage.extract_token_usage``：那个函数面向 LLMResult
    （``llm_output`` / ``generations``），对 AIMessage 直接返回全 0。
    """
    um = getattr(resp, "usage_metadata", None) or {}
    rm = getattr(resp, "response_metadata", None) or {}
    tu = rm.get("token_usage") or rm.get("usage") or {}
    if not isinstance(tu, dict):
        tu = {}
    details = (um.get("input_token_details") if isinstance(um, dict) else None) or {}
    cached = 0
    if isinstance(details, dict):
        cached = int(details.get("cache_read") or details.get("cached_tokens") or 0)
    if not cached:
        ptd = tu.get("prompt_tokens_details") or {}
        if isinstance(ptd, dict):
            cached = int(ptd.get("cached_tokens") or 0)
        cached = cached or int(tu.get("prompt_cache_hit_tokens") or 0)
    inp = int((um.get("input_tokens") if isinstance(um, dict) else 0) or 0)
    out = int((um.get("output_tokens") if isinstance(um, dict) else 0) or 0)
    if not inp:
        inp = int(tu.get("prompt_tokens") or 0)
    if not out:
        out = int(tu.get("completion_tokens") or 0)
    return {"input_tokens": inp, "output_tokens": out, "cached_tokens": cached}


def build_layouts(salt_prefix: str = "") -> dict[str, list[list[tuple[str, str]]]]:
    """每个布局返回「按顺序要发的调用列表」。

    除 L0/L1/L2 外附两组对照，用来把「共享前缀」与「追加续写」两个变量分开：

        C1 完全相同重发 —— 证明该 session 的缓存确实可命中（排除 key/路由问题）
        C2 纯追加       —— 同一份 [背景][步骤A] 之后只 append，证明命中来自「续写」
    """

    def bg(tag: str) -> str:
        return build_background(f"{salt_prefix}{tag}")

    bg0, bg1, bg2, bgc1, bgc2 = bg("L0"), bg("L1"), bg("L2"), bg("C1"), bg("C2")

    l0_write = [("system", bg0), ("user", STEP_A_FULL)]
    l0_read = [("system", bg0), ("user", STEP_B_FULL)]

    l1_write = [("system", bg1), ("system", STEP_A_FULL), ("user", QUESTION_A)]
    l1_read = [("system", bg1), ("system", STEP_B_FULL), ("user", QUESTION_B)]

    l2_write = [("system", bg2), ("system", SYSTEM_RULES), ("user", STEP_A_FULL)]
    # 后两步在拿到上一步的真实输出后再拼（占位符 None，运行时替换）
    l2_read1_placeholder: list[tuple[str, str] | None] = [
        *l2_write, None, ("user", STEP_B_FULL)]
    l2_read2_placeholder: list[tuple[str, str] | None] = [
        *l2_read1_placeholder, None, ("user", STEP_C_FULL)]

    c1_write = [("system", bgc1), ("user", STEP_A_FULL)]
    c1_resend = [("system", bgc1), ("user", STEP_A_FULL)]

    c2_write = [("system", bgc2), ("user", STEP_A_FULL)]
    c2_append: list[tuple[str, str] | None] = [*c2_write, None, ("user", STEP_B_FULL)]

    return {
        "L0": [l0_write, l0_read],
        "L1": [l1_write, l1_read],
        "L2": [l2_write, l2_read1_placeholder, l2_read2_placeholder],  # type: ignore[list-item]
        "C1-完全相同重发": [c1_write, c1_resend],
        "C2-纯追加": [c2_write, c2_append],  # type: ignore[list-item]
    }


def _resolve(messages: list, outputs: list[str]) -> list[tuple[str, str]]:
    """把 L2 的 assistant 占位符替换成上一步的真实输出。"""
    out: list[tuple[str, str]] = []
    oi = 0
    for m in messages:
        if m is None:
            content = outputs[oi] if oi < len(outputs) else "(ok)"
            out.append(("assistant", content))
            oi += 1
        else:
            out.append(m)  # type: ignore[arg-type]
    return out


def _print_dry_run(layouts, max_tokens: int) -> None:
    print("# 干跑：只打印将发送的消息槽与本地前缀 sha（未发任何请求）\n")
    print("| 布局 | 调用 | 消息槽（role:字符数） | 前缀 sha 前16 | 字符数 | ≈token |")
    print("| --- | --- | --- | --- | ---: | ---: |")
    for name, calls in layouts.items():
        for idx, msgs in enumerate(calls, 1):
            resolved = _resolve(msgs, ["(assistant-output)"])
            sha, chars, tok = messages_digest(resolved)
            print(f"| {name} | #{idx} | `{_slot_label(resolved)}` | `{sha}` | {chars} | {tok} |")
    print()
    print(f"max_tokens={max_tokens}（补全极小，成本可忽略）")
    for name, calls in layouts.items():
        if len(calls) >= 2:
            a = _resolve(calls[0], ["(assistant-output)"])
            b = _resolve(calls[1], ["(assistant-output)"])
            cp = common_prefix_chars(a, b)
            print(f"- {name}: 第 1→2 次调用的公共前缀 {cp} 字符 "
                  f"（≈{int(cp / CHARS_PER_TOKEN)} token）；"
                  f"长度 {messages_digest(a)[1]} → {messages_digest(b)[1]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="前缀排布对照探针（L0/L1/L2）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 payload 前缀，不发请求")
    ap.add_argument("--max-tokens", type=int, default=8, help="补全上限（默认 8）")
    ap.add_argument("--gap-seconds", type=float, default=15.0,
                    help="写缓存与读缓存之间的等待（默认 15s；provider 写入有秒级延迟）")
    ap.add_argument("--salt", default="",
                    help="背景 salt 前缀：跨次重跑会命中上次写的缓存，需要干净基线时换一个值")
    ap.add_argument("--json", action="store_true", help="追加机器可读 JSON")
    args = ap.parse_args()

    layouts = build_layouts(args.salt)

    if args.dry_run:
        _print_dry_run(layouts, args.max_tokens)
        return 0

    # 同一 session：new_session() 之后所有请求共用这个 UUID（ContextVar）
    from utils.opencode_session import new_session
    session_id = new_session()

    from utils.llm_factory import get_llm
    llm = get_llm()

    print(f"session = {session_id}  model = {getattr(llm, 'model_name', '?')}  "
          f"base = {getattr(llm, 'openai_api_base', '?')}")
    print(f"gap = {args.gap_seconds}s  max_tokens = {args.max_tokens}\n")

    rows: list[dict] = []
    for name, calls in layouts.items():
        prev: list[tuple[str, str]] | None = None
        prev_input = 0
        outputs: list[str] = []
        for idx, raw in enumerate(calls, 1):
            msgs = _resolve(raw, outputs)
            sha, chars, tok = messages_digest(msgs)
            common = common_prefix_chars(prev, msgs) if prev is not None else 0
            if prev is not None and args.gap_seconds > 0:
                time.sleep(args.gap_seconds)
            t0 = time.time()
            try:
                resp = llm.invoke(msgs, max_tokens=args.max_tokens)
            except Exception as e:  # noqa: BLE001 - 探针要给出可读失败原因
                print(f"\n[{name} #{idx}] 调用失败: {type(e).__name__}: {e}")
                return 2
            dt = time.time() - t0
            content = str(getattr(resp, "content", "") or "")
            u = _usage(resp)
            prov_fp = str((getattr(resp, "response_metadata", None) or {}).get(
                "prefix_sha256", "") or "")
            hit = (u["cached_tokens"] / u["input_tokens"]) if u["input_tokens"] else 0.0
            reuse_prev = (u["cached_tokens"] / prev_input) if prev_input else 0.0
            rows.append({
                "layout": name, "call": idx, "slots": _slot_label(msgs),
                "messages": [{"role": r, "chars": len(c)} for r, c in msgs],
                "input_tokens": u["input_tokens"],
                "prev_input_tokens": prev_input,
                "output_tokens": u["output_tokens"],
                "cached_tokens": u["cached_tokens"],
                "hit_ratio": round(hit, 4),
                "reuse_prev_ratio": round(reuse_prev, 4),
                "common_prefix_chars": common,
                "chars": chars, "estimated_tokens": tok,
                "prefix_sha16": sha,
                "provider_prefix_sha16": prov_fp[:16],
                "elapsed_s": round(dt, 2),
                "assistant_reply": content[:40],
            })
            print(f"[{name} #{idx}] in={u['input_tokens']} cached={u['cached_tokens']} "
                  f"hit={hit:.1%} reuse_prev={reuse_prev:.1%} common_prev={common}字 "
                  f"sha={sha} {dt:.1f}s")
            outputs.append(content if content else "(ok)")
            prev = msgs
            prev_input = u["input_tokens"]

    print("\n## 实测结果\n")
    print("| 布局 | 调用 | 消息槽（role:字符数） | input | cached | 占本次 | 复用上次 | 公共前缀 |")
    print("| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in rows:
        print(f"| {r['layout']} | #{r['call']} | `{r['slots']}` | {r['input_tokens']} | "
              f"{r['cached_tokens']} | {r['hit_ratio']:.1%} | "
              f"{r['reuse_prev_ratio']:.1%} | {r['common_prefix_chars']}字 |")

    print("\n## 布局汇总（第 2 次调用 = 真正的「下一步」）\n")
    print("| 布局 | 第2次 input | 上次 input | cached | 复用上次 | 占本次 | 结论 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for name in layouts:
        second = next((r for r in rows if r["layout"] == name and r["call"] == 2), None)
        if not second:
            continue
        if second["reuse_prev_ratio"] >= 0.95:
            verdict = "整段复用上一次请求（追加式）"
        elif second["cached_tokens"] > 0:
            verdict = "只命中共享前缀"
        else:
            verdict = "0 命中（无共享前缀或未过写入延迟）"
        print(f"| {name} | {second['input_tokens']} | {second['prev_input_tokens']} | "
              f"{second['cached_tokens']} | {second['reuse_prev_ratio']:.1%} | "
              f"{second['hit_ratio']:.1%} | {verdict} |")

    l2_rows = [r for r in rows if r["layout"] == "L2"]
    if len(l2_rows) >= 3:
        print("\n## L2 逐步累积（cached 的增量应≈上一步指令的 token 数）\n")
        print("| 调用 | input | cached | cached 增量 | 公共前缀 |")
        print("| ---: | ---: | ---: | ---: | ---: |")
        last = 0
        for r in l2_rows:
            print(f"| #{r['call']} | {r['input_tokens']} | {r['cached_tokens']} | "
                  f"{r['cached_tokens'] - last} | {r['common_prefix_chars']}字 |")
            last = r["cached_tokens"]

    if args.json:
        print("\n```json")
        print(json.dumps({"session": session_id, "gap_seconds": args.gap_seconds,
                          "rows": rows}, ensure_ascii=False, indent=2))
        print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

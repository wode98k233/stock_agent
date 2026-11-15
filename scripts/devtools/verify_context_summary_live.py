# -*- coding: utf-8 -*-
"""上下文压缩 fork 的**真实 LLM** 验证（会花钱，谨慎运行）。

与 `verify_context_summary_shape.py` 的分工：
- 那个是零成本形态验证（查消息结构对不对）；
- 这个是**真调用**验证，回答只读 trace 回答不了的问题：
  fork 之后 provider 侧到底命中了多少。

用法：
    python scripts/devtools/verify_context_summary_live.py            # 预演，不花钱
    python scripts/devtools/verify_context_summary_live.py --run      # 真调用
    python scripts/devtools/verify_context_summary_live.py --run --rounds 20

⚠️ 每次 --run 约 2~4 次 LLM 调用（预热 + 压缩 + 可能的 JSON 重试），峰时约 ¥0.05。

**取 usage 的路径**（踩过两次坑，记下来）：
  · 走 `run_config` 传 callbacks → `llm_json_with_retry` 不透传，拿不到；
  · 走 `config={"callbacks": [...]}` → LangChain 的 config 合并语义会丢；
  · ✅ 拦 `utils.llm_factory.tracked_invoke`（`llm_json_with_retry` 的唯一出口），
    从返回的 AIMessage 读 `usage_metadata.input_token_details.cache_read`。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

LOG = logging.getLogger("verify-live")


def _content_of(m) -> str:
    """取消息文本（兼容 dict / 元组 / LangChain 消息对象）。"""
    if isinstance(m, dict):
        return str(m.get("content") or "")
    if isinstance(m, (tuple, list)):
        return str(m[1] if len(m) > 1 else "")
    return str(getattr(m, "content", "") or "")


def _usage_of(resp) -> dict:
    """从 AIMessage 抠 input/cached/output。

    实测 `resp.usage_metadata` 形如::

        {'input_tokens': 376, 'output_tokens': 2,
         'input_token_details': {'cache_read': 0}}

    `cache_read` 就是 provider 侧命中的输入 token —— 本次验证唯一要证明的东西。
    """
    um = getattr(resp, "usage_metadata", None) or {}
    details = um.get("input_token_details") or {}
    return {
        "input": int(um.get("input_tokens") or 0),
        "cached": int(details.get("cache_read") or 0),
        "output": int(um.get("output_tokens") or 0),
    }


def _rounds(n: int) -> list:
    """构造 n 个轮次，每轮约 3K 字符（贴近实测 ~11K 字符/轮的体量级）。"""
    msgs = []
    for i in range(1, n + 1):
        msgs.append(AIMessage(content=f"第{i}轮：查板块{i}的行情与资金流", tool_calls=[
            {"name": "mx_data_query", "args": {"q": f"板块{i} 资金流向"}, "id": f"c{i}"}]))
        body = (f"第{i}轮结果：{i}号板块涨跌 {i*0.3:.2f}%，主力净流出 {600+i*7} 亿，"
                f"换手率 {i*0.7:.1f}%。逐只个股明细：")
        body += "；".join(f"股票{j} 收{i*10+j}.{j%10} 涨{(i+j)%5*0.4:.1f}%"
                         f" 成交额{(i+j)*13}亿" for j in range(1, 40))
        msgs.append(ToolMessage(content=body, name="mx_data_query", tool_call_id=f"c{i}"))
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="真调用（默认只预演）")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from config import Config

    tools_res: list = []
    try:
        from tools.skill_register import SkillRegister
        from tools.skills import SkillRegistry
        from utils.memory import MemoryManager
        from utils.logger import get_logger
        lg, *_ = get_logger("verify-live", skip_db=True)
        reg = SkillRegister()
        reg.auto_discover()
        sr = SkillRegistry(lg, MemoryManager(lg), reg)
        tools_res = sr.get_all_tools()
        print(f"装载工具 {len(tools_res)} 个（与生产 full 档一致）")
    except Exception as e:
        print(f"⚠️ 工具装载失败（{e}）→ 退回空 tools，命中率会失真，结果仅供参考")

    msgs = [("system", "你是选股雷达的分析师，按数据回答用户的股票问题。"),
            ("user", "帮我看看今天哪个板块资金流出最多，并给出操作建议")]
    msgs += _rounds(args.rounds)
    est = sum(len(_content_of(m)) for m in msgs) // 3
    print(f"构造 {args.rounds} 轮 / {len(msgs)} 条消息，估算 payload ≈ {est:,} tok")
    print(f"配置：MODE={Config.CONTEXT_SUMMARY_MODE} "
          f"SOFT_LIMIT={Config.REACT_CONTEXT_SOFT_LIMIT_TOKENS} "
          f"ROUNDS={Config.REACT_SUMMARY_TRIGGER_ROUNDS} "
          f"CHARS={Config.REACT_SUMMARY_PENDING_CHARS} "
          f"RECENT={Config.REACT_CONTEXT_RECENT_ROUNDS}")

    if not args.run:
        print("\n[dry-run] 没有真调用。加 --run 执行（约 2~4 次 LLM 调用）。")
        print("预期：压缩调用首次的 cached/input ≥ 0.8（PRD-A11 AC-2）")
        return 0

    from utils.llm_factory import get_llm
    import utils.llm_factory as lf
    llm = get_llm()
    if args.model:
        llm = llm.bind(model=args.model) if hasattr(llm, "bind") else llm

    # ── ① 预热：把前缀写热 ────────────────────────────────────
    print("\n① 预热：发送一次 agent 调用（写热前缀）")
    try:
        warm = llm.invoke(msgs, config={"metadata": {"node": "agent",
                                                    "purpose": "agent"}})
        wu = _usage_of(warm)
        print(f"   OK：{str(getattr(warm, 'content', ''))[:60]}...")
        print(f"   预热 in={wu['input']} cached={wu['cached']}"
              f"（首调用不命中属正常）")
    except Exception as e:
        print(f"   ⚠️ 预热失败：{e}")

    # ── ② 压缩（fork 档），拦 tracked_invoke 抓 usage ──────────
    print("\n② 压缩（fork 档，应命中上面的前缀）")
    import agents.react.context as ctx
    from agents.react.context import (default_summarizer, format_rounds_for_summary,
                                      split_react_rounds_with_spans)
    from agents.shared.prompt_prefix import make_fork_prefix

    captured: dict = {"usages": []}
    real_invoke = lf.tracked_invoke

    def spy_invoke(l, m, logger_, label_, **kw):
        resp = real_invoke(l, m, logger_, label_, **kw)
        captured["usages"].append(_usage_of(resp))
        return resp

    lf.tracked_invoke = spy_invoke
    real_json = ctx.llm_json_with_retry
    captured_msgs: dict = {}

    def spy_json(l, messages, logger, **kw):
        captured_msgs["messages"] = messages
        captured_msgs["meta"] = kw.get("metadata") or {}
        return real_json(l, messages, logger, **kw)

    ctx.llm_json_with_retry = spy_json
    try:
        rounds_obj, _ = split_react_rounds_with_spans(msgs)
        keep = int(Config.REACT_CONTEXT_RECENT_ROUNDS or 0)
        pending = [r for r in rounds_obj if r.index <= args.rounds - keep]
        result = default_summarizer(
            None, format_rounds_for_summary(pending), llm=llm, logger=LOG,
            max_chars=Config.REACT_SUMMARY_MAX_CHARS,
            prior_count=0, current_count=pending[-1].index if pending else 0,
            fork_prefix=make_fork_prefix(msgs, tools_res, source="verify"),
            compress_reason="verify")
    finally:
        ctx.llm_json_with_retry = real_json
        lf.tracked_invoke = real_invoke

    print(f"   返回 JSON 字段："
          f"{sorted(result.keys()) if isinstance(result, dict) else result}")
    meta = captured_msgs.get("meta") or {}
    print(f"   埋点：{json.dumps({k: v for k, v in meta.items() if k.startswith('fork') or k == 'compress_reason'}, ensure_ascii=False)}")

    usages = captured["usages"]
    print(f"\n   压缩调用 {len(usages)} 次（>1 说明 JSON 重试了，重试要额外花钱）")
    for i, u in enumerate(usages):
        hit = u["cached"] / u["input"] if u["input"] else 0
        print(f"     {'首次' if i == 0 else f'重试{i}'}: in={u['input']} "
              f"cached={u['cached']} out={u['output']} 命中={hit:.1%}")

    # ── ③ 判据 ───────────────────────────────────────────────
    print("\n③ 判据（PRD-A11 AC-2 / FR-1）")
    ok = True
    n_sent = len(captured_msgs.get("messages") or [])
    c1 = n_sent == len(msgs) + 1
    ok &= c1
    print(f"   · 形态 {n_sent} 条 = 前缀 {len(msgs)} + 尾部 1 → {'✅' if c1 else '❌'}")
    ft = meta.get("fork_tools")
    ok &= bool(ft)
    print(f"   · tools 绑定 {ft} → {'✅' if ft else '❌'}")
    fk = meta.get("fork_kind")
    ok &= fk == "llm_fork"
    print(f"   · fork_kind={fk} → {'✅' if fk == 'llm_fork' else '❌'}")
    tail = _content_of((captured_msgs.get("messages") or [None])[-1])
    c4 = "上下文归档" in tail
    ok &= c4
    print(f"   · 尾部是归档指令（{len(tail)} 字符，非 pending 全文）→ {'✅' if c4 else '❌'}")
    if usages and usages[0]["input"]:
        hit = usages[0]["cached"] / usages[0]["input"]
        c5 = hit >= 0.8
        ok &= c5
        print(f"   · 首次压缩命中率 {hit:.1%}（AC-2 ≥80%）→ {'✅' if c5 else '❌'}")
    else:
        print("   · 命中率：⚠️ 未抓到 usage，无法判定")
        ok = False

    print(f"\n{'✅ 全部通过' if ok else '❌ 有未通过项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

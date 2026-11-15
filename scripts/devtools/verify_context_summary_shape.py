# -*- coding: utf-8 -*-
"""新上下文结构的**端到端形态验证**（零 LLM 调用，不依赖任何历史 trace）。

与单测的分工：
- 单测（test_context_summary_fork.py）锁**逐条约束**；
- 本脚本验**整体形态**：跑一次真实的压缩流程，把三次 LLM 调用的
  (模型, messages, metadata, tools) 全打出来，肉眼确认「前缀共享 + 尾部追加」成立。

用法：
    python scripts/devtools/verify_context_summary_shape.py
    python scripts/devtools/verify_context_summary_shape.py --recent 2
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, ToolMessage   # noqa: E402

CAPTURED: list[dict] = []


def _install_capture():
    """替换 llm_json_with_retry，把每次调用的完整形态抓下来。"""
    import agents.react.context as ctx

    def fake(llm, messages, logger, **kw):
        # ⚠️ bind_tools 返回的是克隆体（RunnableBinding），绑定信息在 .kwargs["tools"]，
        # 不在 .bound —— 读错字段会误报「未绑定」。
        bound = (getattr(llm, "kwargs", None) or {}).get("tools")
        CAPTURED.append({
            "llm": llm,
            "messages": messages,
            "metadata": kw.get("metadata") or {},
            "bound_tools": bound,
        })
        return {
            "summary_version": 1,
            "summarized_rounds": [1, 2],
            "rounds_summary": "第1轮查指数，第2轮查资金",
            "key_findings": "上证 3250.45，主力净流出 637 亿",
            "data_coverage": ["大盘行情", "资金流向"],
            "failed_queries": [],
        }

    ctx.llm_json_with_retry = fake


class _FakeLLM:
    """记录 bind_tools 调用，便于确认 tools 是否真的绑上了。"""
    def __init__(self, name="main"):
        self.name = name
        self.kwargs = {}
        self.bound = None

    def bind_tools(self, tools):
        self.bound = tools
        clone = _FakeLLM(self.name)
        clone.kwargs = {"tools": tools}
        return clone

    def __repr__(self):
        return f"<LLM {self.name} bound={len(self.bound) if self.bound else None}>"


class _Exec:
    def __init__(self):
        self.react_context_summaries = []
        self.react_context_summarized_until = 0
        self.react_context_summary_updates = 0


def _rounds(n: int) -> list:
    msgs = []
    for i in range(1, n + 1):
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "mx_data_query", "args": {"q": f"查询{i}"}, "id": f"c{i}"}]))
        msgs.append(ToolMessage(content=f"第{i}轮结果：上证{3250+i}，主力净流出{600+i}亿" + "明细" * 40,
                                name="mx_data_query", tool_call_id=f"c{i}"))
    return msgs


def _fp(msgs) -> str:
    """整个消息序列的指纹（判断两个调用是否共享前缀）。"""
    parts = []
    for m in msgs:
        if isinstance(m, dict):
            parts.append(f"{m.get('role')}|{m.get('content')}")
        else:
            parts.append(f"{getattr(m, 'type', m[0])}|{m.get('content') if hasattr(m, 'content') else m[1]}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _shared_prefix_len(a, b) -> int:
    n = 0
    for x, y in zip(a, b):
        sx = x.get("content") if isinstance(x, dict) else str(getattr(x, "content", ""))
        sy = y.get("content") if isinstance(y, dict) else str(getattr(y, "content", ""))
        if sx != sy:
            break
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=1,
                    help="保留最近几轮不压（0 = 全部压掉）")
    ap.add_argument("--rounds", type=int, default=3, help="构造多少个轮次")
    ap.add_argument("--mode", default="fork", choices=["fork", "standalone"])
    args = ap.parse_args()

    _install_capture()
    from agents.react.context import ReactContextBuilder
    from config import Config
    import logging

    Config.CONTEXT_SUMMARY_MODE = args.mode
    logging.basicConfig(level=logging.WARNING)

    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(52)]
    llm = _FakeLLM("main")
    messages = [("system", "REACT_SYSTEM_PROMPT"), ("user", "今日大盘总结")] + _rounds(args.rounds)

    builder = ReactContextBuilder(
        recent_rounds=args.recent,
        summary_trigger_rounds=2,           # 构造的轮次数 > 它 → 必触发
        summary_pending_chars=10_000_000,   # 关掉字符阈值，只让轮次触发
        summary_max_chars=2500,
    )
    out = builder.build(messages=list(messages), user_input="今日大盘总结",
                        exec_state=_Exec(), llm=llm, logger=logging.getLogger("verify"),
                        tools=tools)

    print(f"模式={args.mode}  轮次={args.rounds}  recent_rounds={args.recent}")
    print(f"输入 {len(messages)} 条 → 输出 {len(out)} 条\n")

    if not CAPTURED:
        print("❌ 压缩没有触发")
        return 1
    call = CAPTURED[0]
    meta = call["metadata"]
    print("== 压缩调用形态 ==")
    print(f"  模型            : {call['llm']}")
    print(f"  绑定 tools      : {len(call['bound_tools']) if call['bound_tools'] else '❌ 未绑定'}")
    print(f"  fork_kind       : {meta.get('fork_kind')}")
    print(f"  fork_prefix_chars: {meta.get('fork_prefix_chars')}")
    print(f"  fork_prefix_msgs : {meta.get('fork_prefix_msgs')}")
    print(f"  fork_skip_reason : {meta.get('fork_skip_reason', '-')}")
    print(f"  compress_reason  : {meta.get('compress_reason')}")

    tail = call["messages"][-1]
    tail_text = tail.get("content") if isinstance(tail, dict) else tail[1]
    print(f"  尾部角色/长度   : {tail.get('role') if isinstance(tail, dict) else tail[0]} / {len(tail_text)} 字符")

    ok = True
    print("\n== 判据 ==")
    if not call["bound_tools"] and args.mode == "fork":
        print("  ⚠️ tools 未绑定 —— 脚本读的是 llm.kwargs['tools']（bind_tools 的克隆体），"
              "若这里报错说明生产代码的 bind_tools 没生效")
    if args.mode == "fork":
        shared = _shared_prefix_len(call["messages"], messages)
        print(f"  ① 前缀与 agent payload 共享 {shared}/{len(messages)} 条消息"
              f" → {'✅ 全命中' if shared == len(messages) else '⚠️ 未全覆盖'}")
        ok &= shared == len(messages)
        bound = call["bound_tools"]
        print(f"  ② tools 已绑定 {len(bound) if bound else 0} 个 → "
              f"{'✅' if bound else '❌ tools 数组是缓存键的一部分，漏绑则前缀归零'}")
        ok &= bool(bound)
        print(f"  ③ 尾部是归档指令而非 pending 全文 → "
              f"{'✅' if '上下文归档' in tail_text else '❌'}")
        ok &= "上下文归档" in tail_text
        print(f"  ④ 尾部长度 {len(tail_text)} 字符（pending 全文约 {len(str(messages))}）"
              f" → {'✅ 省下大头' if len(tail_text) < 1000 else '⚠️ 尾部偏大'}")
    else:
        first = call["messages"][0]
        first_role = first[0] if isinstance(first, (tuple, list)) else first.get("role")
        first_body = first[1] if isinstance(first, (tuple, list)) else first.get("content", "")
        is_summary_sys = first_role == "system" and "上下文压缩助手" in str(first_body)
        second = call["messages"][1]
        second_body = second[1] if isinstance(second, (tuple, list)) else second.get("content", "")
        has_pending = "ROUNDS" in str(second_body) or "轮" in str(second_body)
        print(f"  ① 首条是 SUMMARY_SYSTEM_PROMPT → {'✅' if is_summary_sys else '❌'}")
        print(f"  ② pending 全文在第 2 条 user 里 → {'✅' if has_pending else '❌'}")
        ok &= is_summary_sys and has_pending

    print(f"\n摘要是否写回 exec_state：是（_maybe_summarize 推进游标）")
    print(f"压缩后消息尾部：{type(out[-1]).__name__}")
    print(f"\n{'✅ 通过' if ok else '❌ 不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
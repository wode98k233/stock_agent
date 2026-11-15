#!/usr/bin/env python
"""只读探针：classify / clarify 实际发出的 messages 形态与共享前缀（前缀对齐改造验收）。

背景（前缀缓存口径）：provider 的前缀缓存从 payload 第 0 字节开始连续比对
（无 tools 时即 messages[0]；classify / clarify 都属于这一族）。
两者收到的「共享块」（agents/base.py::_enrich_user_input 的增强输入）在**同一 run
内逐字节相同**，但改前各自把调用指令压在 messages[0]、共享块被挡在 system 之后，
于是两族之间恒 0% 命中。

用法::

    # 改动前：记录基线
    python scripts/devtools/probe_classify_clarify_prefix.py --record <before.json>
    # 改动后：对照基线断言不变性
    python scripts/devtools/probe_classify_clarify_prefix.py --check <before.json>
    # 只看当前形态
    python scripts/devtools/probe_classify_clarify_prefix.py

--check 断言（缺一即失败，退出码 1）::

    1. 共享块文本 md5 前后一致
    2. 调用指令文本 md5 前后一致
    3. 每条消息的（md5, 字符数）**多重集**前后一致、角色计数一致、各 case 字符总数/条数
       不变 → 证明是「搬位置（含 system<->user 角色互换）」而不是「复制」
    4. classify 与 clarify 改后第 0 条逐字节相同；改前最大共享前缀为 0 字符

只读：不调用真实 LLM（拦截 utils.llm_factory.llm_json_with_retry）；
除 --record 写目标 JSON 外不写任何文件。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

# Windows 默认控制台可能是 GBK：固定 UTF-8 输出，避免探针自身被编码问题打断。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_BASE = Path(__file__).resolve().parents[3]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# 用于在消息序列里定位「调用指令」与「共享块」的锚点
CLASSIFY_ANCHOR = "判断用户问题是否属于股票/金融分析范畴"
TITLE_ANCHOR = "**附加任务**"
CLARIFY_ANCHOR = "你是选股雷达的澄清助手"

# 与实测 trace 同量级的共享块（4,227 字符）；刻意包含花括号 JSON，
# 锁住「共享块必须走 {input} 变量、不能被 LangChain 当模板解析」这条实现约束。
SHARED_TARGET_CHARS = 4227

_ZERO_HIST = "classify/无标题/0轮历史"
_ZERO_HIST_Q = "clarify/无标题/0轮历史"
_HIST = "classify/无标题/2轮历史"
_HIST_Q = "clarify/无标题/2轮历史"


def _shared_block() -> str:
    head = ("[背景信息] 当前时间：2026-10-18 09:31（盘中）\n"
            "[用户问题] 帮我看看红利低波ETF最近为什么跌\n"
            "[市场状态] 正常开市")
    profile = "## 用户画像\n- 风险偏好：稳健型\n- 常看板块：红利、银行"
    memory = ('[相关记忆片段]\n- 上次问过：红利低波ETF 的定投节奏 '
              '{"risk": "low", "horizon": "1y", "amount": 20000}')
    text = "\n".join([head, profile, memory])
    filler = "\n- 记忆填充：用户关注高股息与低波动因子组合，讨论过按月定投与回撤控制。"
    while len(text) < SHARED_TARGET_CHARS:
        text += filler
    return text[:SHARED_TARGET_CHARS]


def _history(rounds: int):
    """与 MemoryManager.get_history() 同形：LangChain 消息对象列表。"""
    from langchain_core.messages import AIMessage, HumanMessage

    out: list = []
    for i in range(rounds):
        out.append(HumanMessage(content=f"[背景信息] …历史第{i + 1}轮…\n[用户问题] 历史提问{i + 1}"))
        out.append(AIMessage(content=f"历史回答{i + 1}"))
    return out


class _Mem:
    def __init__(self, history):
        self._history = list(history)

    def get_history(self):
        return list(self._history)

    def append_turn(self, *a, **k):
        pass


class _Logger:
    def isEnabledFor(self, *a, **k):
        return False

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def phase(self, *a, **k):
        pass


def _capture(call: str, shared: str, rounds: int, with_title: bool):
    """跑一次真实装配路径，返回**实际发出**的 messages（拦截 LLM 网关入口）。"""
    from utils import llm_factory

    captured: dict = {"messages": []}

    def _fake_json(llm, messages, logger, **kw):
        captured["messages"] = list(messages)
        # is_stock_related=False → classify_input 在其后立即返回，
        # 不再走策略会话 / 澄清分支（探针保持零副作用、零 DB 访问）。
        return {"is_stock_related": False, "response": "（探针）非股票问题"}

    token = None
    orig = llm_factory.llm_json_with_retry
    llm_factory.llm_json_with_retry = _fake_json
    try:
        if call == "classify":
            from agents import common
            if with_title:
                from agents.shared.title_ctx import (TitleRequest, reset_title_request,
                                                     set_title_request)
                token = set_title_request(TitleRequest(
                    first_question="红利低波ETF为什么跌", callback=lambda _t: None))
            try:
                asyncio.run(common.classify_input(shared, _Mem(_history(rounds)),
                                                  object(), _Logger()))
            finally:
                if token is not None:
                    reset_title_request(token)
        else:
            from agents.shared import clarify
            clarify._generate_questions(None, _Mem(_history(rounds)), _Logger(),
                                        shared, None, None, None)
    finally:
        llm_factory.llm_json_with_retry = orig
    return captured["messages"]


def _parts(msg) -> tuple[str, str]:
    if isinstance(msg, tuple) and len(msg) == 2:
        return str(msg[0]), str(msg[1])
    role = getattr(msg, "type", type(msg).__name__)
    role = {"human": "user", "ai": "assistant"}.get(role, role)
    content = getattr(msg, "content", "")
    return str(role), content if isinstance(content, str) else str(content)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _snapshot(call: str, rounds: int, with_title: bool, shared: str) -> dict:
    messages = _capture(call, shared, rounds, with_title)
    parts = [_parts(m) for m in messages]
    anchor = CLASSIFY_ANCHOR if call == "classify" else CLARIFY_ANCHOR

    shared_idx = next(i for i, (_r, c) in enumerate(parts) if c == shared)
    instr_idx = next(i for i, (_r, c) in enumerate(parts) if anchor in c)
    instr_text = parts[instr_idx][1]

    return {
        "label": f"{call}/{'带标题' if with_title else '无标题'}/{rounds}轮历史",
        "call": call,
        "with_title": with_title,
        "history_rounds": rounds,
        "shared_idx": shared_idx,
        "shared_role": parts[shared_idx][0],
        "shared_md5": _md5(shared),
        "shared_chars": len(shared),
        "instr_idx": instr_idx,
        "instr_role": parts[instr_idx][0],
        "instr_md5": _md5(instr_text),
        "instr_chars": len(instr_text),
        "instr_head": instr_text[:60].replace("\n", " "),
        "has_title_tag": TITLE_ANCHOR in instr_text,
        "msg0_md5": _md5(parts[0][1]),
        "msg_count": len(parts),
        "total_chars": sum(len(c) for _r, c in parts),
        "messages": [{"role": r, "md5": _md5(c), "chars": len(c)} for r, c in parts],
    }


def _cases(shared_block: str) -> list[dict]:
    out = []
    for call, with_title, rounds in [
        ("classify", False, 0),
        ("classify", False, 2),
        ("classify", True, 0),
        ("clarify", False, 0),
        ("clarify", False, 2),
    ]:
        out.append(_snapshot(call, rounds, with_title, shared_block))
    return out


def _by_label(cases: list[dict]) -> dict:
    return {s["label"]: s for s in cases}


def _shared_prefix(a: dict, b: dict) -> tuple[int, int]:
    """两条消息序列的最大逐字节共享前缀（字符数, 消息条数）。"""
    chars = msgs = 0
    for x, y in zip(a["messages"], b["messages"]):
        if x["md5"] != y["md5"]:
            break
        chars += x["chars"]
        msgs += 1
    return chars, msgs


def _print_cases(title: str, cases: list[dict]) -> None:
    print(f"\n=== {title} ===")
    for s in cases:
        print(f"\n[{s['label']}] 消息 {s['msg_count']} 条 / 合计 {s['total_chars']} 字符")
        for i, m in enumerate(s["messages"]):
            tags = []
            if i == s["shared_idx"]:
                tags.append("<- 共享块")
            if i == s["instr_idx"]:
                tags.append("<- 调用指令")
            print(f"    #{i} {m['role']:<9} {m['chars']:>6} 字符 "
                  f"md5={m['md5'][:12]} {' '.join(tags)}")


def _print_prefix(cur: dict) -> None:
    print("\n=== 共享前缀实测（classify <-> clarify）===")
    for name, la, lb in [("无历史", _ZERO_HIST, _ZERO_HIST_Q),
                         ("含 2 轮历史", _HIST, _HIST_Q)]:
        a, b = cur[la], cur[lb]
        chars, msgs = _shared_prefix(a, b)
        print(f"  {name}：逐字节相同的头 {msgs} 条消息 / {chars} 字符"
              f"（共享块下标 classify #{a['shared_idx']} / clarify #{b['shared_idx']}）")
    c0, q0 = cur[_ZERO_HIST], cur[_ZERO_HIST_Q]
    print(f"  第 0 条逐字节相同：{'是' if c0['msg0_md5'] == q0['msg0_md5'] else '否'}")


def _fail(errors: list[str], msg: str) -> None:
    errors.append(msg)
    print(f"  [FAIL] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="classify/clarify 消息形态与共享前缀探针（只读）")
    ap.add_argument("--record", metavar="PATH", help="记录基线 JSON")
    ap.add_argument("--check", metavar="PATH", help="对照基线 JSON 断言不变性")
    args = ap.parse_args()

    shared = _shared_block()
    print(f"共享块：{len(shared)} 字符 md5={_md5(shared)}"
          f"（与实测 trace 同量级；含花括号 JSON，锁模板转义）")

    cur_list = _cases(shared)
    cur = _by_label(cur_list)
    _print_cases("当前实际发出的 messages", cur_list)
    _print_prefix(cur)

    if args.record:
        Path(args.record).write_text(
            json.dumps({"shared_md5": _md5(shared), "shared_chars": len(shared),
                        "cases": cur_list}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n基线已写入 {args.record}")

    if args.check:
        base = json.loads(Path(args.check).read_text(encoding="utf-8"))
        bmap = _by_label(base["cases"])
        errors: list[str] = []
        print(f"\n=== 对照基线 {args.check} ===")
        if base["shared_md5"] != _md5(shared):
            _fail(errors, f"共享块 md5 变化：{base['shared_md5']} -> {_md5(shared)}")
        for label, s in cur.items():
            b = bmap.get(label)
            if b is None:
                _fail(errors, f"基线缺 case {label}")
                continue
            if b["shared_md5"] != s["shared_md5"]:
                _fail(errors, f"[{label}] 共享块 md5 变了")
            if b["instr_md5"] != s["instr_md5"]:
                _fail(errors, f"[{label}] 调用指令 md5 变了")
            if b["total_chars"] != s["total_chars"] or b["msg_count"] != s["msg_count"]:
                _fail(errors, f"[{label}] 字符总数/条数变了："
                              f"{b['total_chars']}/{b['msg_count']} -> "
                              f"{s['total_chars']}/{s['msg_count']}")
            # 文本多重集（md5, 字符数）必须逐项相同 —— 这是「搬位置、不复制」的判据；
            # 角色是**故意互换**的（system<->user），故单独只比角色计数。
            tb = sorted((m["md5"], m["chars"]) for m in b["messages"])
            tc = sorted((m["md5"], m["chars"]) for m in s["messages"])
            if tb != tc:
                _fail(errors, f"[{label}] 消息文本多重集（md5,字符数）不一致（疑似复制/改写）")
            rb = sorted(m["role"] for m in b["messages"])
            rc = sorted(m["role"] for m in s["messages"])
            if rb != rc:
                _fail(errors, f"[{label}] 消息角色计数变了 {rb} -> {rc}")

        # 4. 共享前缀：改前 0 字符 -> 改后共享块起头
        for name, la, lb in [("无历史", _ZERO_HIST, _ZERO_HIST_Q),
                             ("含 2 轮历史", _HIST, _HIST_Q)]:
            bc, bq = bmap[la], bmap[lb]
            b_chars, b_msgs = _shared_prefix(bc, bq)
            a_chars, a_msgs = _shared_prefix(cur[la], cur[lb])
            print(f"  {name}：改前共享前缀 {b_msgs} 条 / {b_chars} 字符"
                  f" -> 改后 {a_msgs} 条 / {a_chars} 字符")
            if b_chars != 0:
                _fail(errors, f"[{name}] 改前共享前缀应为 0 字符，实际 {b_chars}")
            if a_msgs < 1 or a_chars < len(shared):
                _fail(errors, f"[{name}] 改后共享前缀不足一个共享块（{a_chars} < {len(shared)}）")

        c0, q0 = cur[_ZERO_HIST], cur[_ZERO_HIST_Q]
        print(f"  改前形态：共享块 classify #{bmap[_ZERO_HIST]['shared_idx']} "
              f"/ clarify #{bmap[_ZERO_HIST_Q]['shared_idx']}，"
              f"指令 classify #{bmap[_ZERO_HIST]['instr_idx']} "
              f"/ clarify #{bmap[_ZERO_HIST_Q]['instr_idx']}")
        print(f"  改后形态：共享块 #{c0['shared_idx']}（role={c0['shared_role']}），"
              f"指令 #{c0['instr_idx']}（role={c0['instr_role']}）")
        if c0["shared_md5"] != q0["shared_md5"] or c0["msg0_md5"] != q0["msg0_md5"]:
            _fail(errors, "改后第 0 条不是同一段共享块")
        if c0["shared_role"] != "system" or c0["instr_role"] != "user":
            _fail(errors, f"改后形态不符：[{c0['shared_role']}] 共享块 + "
                          f"[{c0['instr_role']}] 指令")

        print("\n=== 结论 ===")
        if errors:
            print(f"  [FAIL] 失败 {len(errors)} 项")
            for e in errors:
                print(f"    - {e}")
            return 1
        print("  [OK] 共享块与调用指令文本 md5 均未变；每条消息字符数集合与总数不变"
              "（搬位置、未复制）")
        print(f"  [OK] classify / clarify 第 0 条逐字节相同，共享前缀 "
              f"{len(shared)} 字符（含历史时再加历史消息）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

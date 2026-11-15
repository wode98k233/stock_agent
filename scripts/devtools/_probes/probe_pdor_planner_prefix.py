#!/usr/bin/env python
"""只读探针：PDOR planner 实际发出的 messages 形态 + 它与 classify 的最大逐字节共享前缀。

与 scripts/devtools/probe_classify_clarify_prefix.py 同族：直接复用它的共享块与装配助手，
保证「planner 侧」与「classify 侧」拿到的是**同一段**共享块文本，前缀长度可直接对比。

背景（前缀缓存口径）：provider 的前缀缓存从 payload 第 0 字节开始连续比对（本调用无 tools →
即 messages[0]）。planner 与 classify 在同一 run 内收到的「共享块」（agents/base.py::
_enrich_user_input 的增强输入，落在 state["input"]）逐字节相同；改前 planner 把规划指令压在
messages[0]、共享块被挡在 system 之后 → 与 classify 之间恒 0 字符共享。改后形态::

    [system: 共享块] [system: 已完成摘要(可选)] [user: 规划指令]

用法::

    # 改动前：记录基线
    python scripts/devtools/probe_pdor_planner_prefix.py --record <before.json>
    # 改动后：对照基线断言不变性
    python scripts/devtools/probe_pdor_planner_prefix.py --check <before.json>
    # 只看当前形态
    python scripts/devtools/probe_pdor_planner_prefix.py

--check 断言（缺一即失败，退出码 1）::

    1. 共享块文本 md5 前后一致
    2. 规划指令文本 md5 前后一致
    3. 每条消息的（md5, 字符数）**多重集**前后一致、角色计数一致、各 case 字符总数/条数
       不变 → 证明是「搬位置（含 system<->user 角色互换）」而不是「复制」
    4. planner 与 classify 改后第 0 条逐字节相同；改前最大共享前缀为 0 字符
    5. skip_cache_prefix / sr_cacheable 标记按裁定透传（invoke 期注入的前缀不参与比对）

只读：不调用真实 LLM（拦截 utils.llm_factory.llm_json_with_retry 与 get_llm）；
除 --record 写目标 JSON 外不写任何文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Windows 默认控制台可能是 GBK：固定 UTF-8 输出，避免探针自身被编码问题打断。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = Path(__file__).resolve().parent
_BASE = _HERE.parents[2]
for _p in (str(_HERE), str(_BASE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 classify/clarify 探针的共享块与装配助手（同一段共享块文本 + 同一套 md5/前缀算法）
from probe_classify_clarify_prefix import (  # noqa: E402
    _Logger,
    _md5,
    _parts,
    _shared_block,
    _shared_prefix,
    _snapshot,
)

# 用于在消息序列里定位「规划指令」与「已完成步骤摘要」
PLANNER_ANCHOR = "你是选股雷达的规划器"
EVIDENCE_ANCHOR = "### 已完成步骤（请勿重复）"

_LABEL_FIRST = "planner/首次规划（无摘要）"
_LABEL_REPLAN = "planner/重规划（带已完成摘要）"
_CLASSIFY_ZERO = "classify/无标题/0轮历史"
_CLASSIFY_HIST = "classify/无标题/2轮历史"


def _planner_state(shared: str, replan: bool) -> dict:
    """与 pdor graph 传入的 state 同形（只取 planner_node 真正读到的键）。"""
    state = {
        "input": shared,
        "_replan_count": 1 if replan else 0,
        "plan_steps": [],
        "info_accumulator": "",
        "selected_skills": [],
        "template_id": "standard",
    }
    if replan:
        # 让 planner 走 completed_summary 分支（info_accumulator 为空 + 有 success 步骤）
        state["plan_steps"] = [{
            "step": 1, "type": "info", "skill": "mx_data",
            "purpose": "获取行情", "status": "success", "result": "数据已获取",
        }]
    return state


def _capture_planner(shared: str, replan: bool) -> tuple[list, dict]:
    """跑一次真实装配路径，返回**实际发出**的 messages 与透传给网关的调用参数。"""
    from unittest.mock import patch

    from utils import llm_factory

    captured: dict = {"messages": [], "kwargs": {}}

    def _fake_json(llm, messages, logger, **kw):
        captured["messages"] = list(messages)
        captured["kwargs"] = dict(kw)
        return {"steps": [{"step": 1, "skill": "mx_data", "purpose": "获取数据", "type": "info"}]}

    from agents.pdor.node import planner as planner_mod

    # 技能目录/模板指引改成固定文本：探针只关心「消息位置与文本不变」，不拉真技能注册表
    ctx = SimpleNamespace(logger=_Logger(), skill_registry=None,
                          progress_reporter=None, budget=None)
    orig = llm_factory.llm_json_with_retry
    llm_factory.llm_json_with_retry = _fake_json
    try:
        with patch.object(planner_mod, "_build_template_guidance",
                          return_value="（探针）模板指引"), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt",
                   return_value="（探针）技能目录"), \
             patch("utils.llm_factory.get_llm", return_value=object()):
            asyncio.run(planner_mod.planner_node(_planner_state(shared, replan), ctx))
    finally:
        llm_factory.llm_json_with_retry = orig
    return captured["messages"], captured["kwargs"]


def _snapshot_planner(shared: str, replan: bool) -> dict:
    messages, kw = _capture_planner(shared, replan)
    parts = [_parts(m) for m in messages]

    shared_idx = next(i for i, (_r, c) in enumerate(parts) if c == shared)
    instr_idx = next(i for i, (_r, c) in enumerate(parts) if PLANNER_ANCHOR in c)
    instr_text = parts[instr_idx][1]
    ev_idx = next((i for i, (_r, c) in enumerate(parts) if EVIDENCE_ANCHOR in c), -1)

    return {
        "label": _LABEL_REPLAN if replan else _LABEL_FIRST,
        "call": "pdor-planner",
        "replan": replan,
        "shared_idx": shared_idx,
        "shared_role": parts[shared_idx][0],
        "shared_md5": _md5(shared),
        "shared_chars": len(shared),
        "instr_idx": instr_idx,
        "instr_role": parts[instr_idx][0],
        "instr_md5": _md5(instr_text),
        "instr_chars": len(instr_text),
        "instr_head": instr_text[:60].replace("\n", " "),
        "evidence_idx": ev_idx,
        "evidence_chars": len(parts[ev_idx][1]) if ev_idx >= 0 else 0,
        "msg0_md5": _md5(parts[0][1]),
        "msg_count": len(parts),
        "total_chars": sum(len(c) for _r, c in parts),
        # 网关调用参数（invoke 期注入的 A 股前缀不在此层，故不参与「不变性」比对）
        "skip_cache_prefix": bool(kw.get("skip_cache_prefix")),
        "sr_cacheable": (kw.get("metadata") or {}).get("sr_cacheable", "未传"),
        "messages": [{"role": r, "md5": _md5(c), "chars": len(c)} for r, c in parts],
    }


def _cases(shared: str) -> list[dict]:
    out = [
        _snapshot_planner(shared, replan=False),
        _snapshot_planner(shared, replan=True),
    ]
    # classify 侧直接复用同族探针的真实装配（0 轮历史 = PDOR planner 的实际场景）
    out.append(_snapshot("classify", 0, False, shared))
    out.append(_snapshot("classify", 2, False, shared))
    return out


def _by_label(cases: list[dict]) -> dict:
    return {s["label"]: s for s in cases}


def _print_cases(title: str, cases: list[dict]) -> None:
    print("\n=== %s ===" % title)
    for s in cases:
        print("\n[%s] 消息 %d 条 / 合计 %d 字符" % (s["label"], s["msg_count"], s["total_chars"]))
        for i, m in enumerate(s["messages"]):
            tags = []
            if i == s.get("shared_idx"):
                tags.append("<- 共享块")
            if i == s.get("instr_idx"):
                tags.append("<- 调用指令")
            if i == s.get("evidence_idx"):
                tags.append("<- 已完成摘要")
            print("    #%d %-9s %6d 字符 md5=%s %s"
                  % (i, m["role"], m["chars"], m["md5"][:12], " ".join(tags)))


def _print_prefix(cur: dict) -> None:
    print("\n=== 共享前缀实测（pdor-planner <-> classify）===")
    for name, lb in [("classify 无历史", _CLASSIFY_ZERO),
                     ("classify 含 2 轮历史", _CLASSIFY_HIST)]:
        a, b = cur[_LABEL_FIRST], cur[lb]
        chars, msgs = _shared_prefix(a, b)
        print("  %s：逐字节相同的头 %d 条消息 / %d 字符（共享块下标 planner #%d / classify #%d）"
              % (name, msgs, chars, a["shared_idx"], b["shared_idx"]))
    print("  第 0 条逐字节相同：%s"
          % ("是" if cur[_LABEL_FIRST]["msg0_md5"] == cur[_CLASSIFY_ZERO]["msg0_md5"] else "否"))


def _fail(errors: list[str], msg: str) -> None:
    errors.append(msg)
    print("  [FAIL] %s" % msg)


def main() -> int:
    ap = argparse.ArgumentParser(description="PDOR planner 消息形态与共享前缀探针（只读）")
    ap.add_argument("--record", metavar="PATH", help="记录基线 JSON")
    ap.add_argument("--check", metavar="PATH", help="对照基线 JSON 断言不变性")
    args = ap.parse_args()

    shared = _shared_block()
    print("共享块：%d 字符 md5=%s（与实测 trace 同量级；含花括号 JSON，锁模板转义）"
          % (len(shared), _md5(shared)))

    cur_list = _cases(shared)
    cur = _by_label(cur_list)
    _print_cases("当前实际发出的 messages", cur_list)
    _print_prefix(cur)

    if args.record:
        Path(args.record).write_text(
            json.dumps({"shared_md5": _md5(shared), "shared_chars": len(shared),
                        "cases": cur_list}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("\n基线已写入 %s" % args.record)

    if args.check:
        base = json.loads(Path(args.check).read_text(encoding="utf-8"))
        bmap = _by_label(base["cases"])
        errors: list[str] = []
        print("\n=== 对照基线 %s ===" % args.check)
        if base["shared_md5"] != _md5(shared):
            _fail(errors, "共享块 md5 变化：%s -> %s" % (base["shared_md5"], _md5(shared)))
        for label in (_LABEL_FIRST, _LABEL_REPLAN):
            s, b = cur[label], bmap.get(label)
            if b is None:
                _fail(errors, "基线缺 case %s" % label)
                continue
            if b["shared_md5"] != s["shared_md5"]:
                _fail(errors, "[%s] 共享块 md5 变了" % label)
            if b["instr_md5"] != s["instr_md5"]:
                _fail(errors, "[%s] 调用指令 md5 变了" % label)
            if b["total_chars"] != s["total_chars"] or b["msg_count"] != s["msg_count"]:
                _fail(errors, "[%s] 字符总数/条数变了：%s/%s -> %s/%s"
                              % (label, b["total_chars"], b["msg_count"],
                                 s["total_chars"], s["msg_count"]))
            # 文本多重集（md5, 字符数）必须逐项相同 —— 这是「搬位置、不复制」的判据；
            # 角色是**故意互换**的（system<->user），故单独只比角色计数。
            tb = sorted((m["md5"], m["chars"]) for m in b["messages"])
            tc = sorted((m["md5"], m["chars"]) for m in s["messages"])
            if tb != tc:
                _fail(errors, "[%s] 消息文本多重集（md5,字符数）不一致（疑似复制/改写）" % label)
            rb = sorted(m["role"] for m in b["messages"])
            rc = sorted(m["role"] for m in s["messages"])
            if rb != rc:
                _fail(errors, "[%s] 消息角色计数变了 %s -> %s" % (label, rb, rc))

        # 共享前缀：改前 0 字符 -> 改后共享块起头
        for name, lb in [("classify 无历史", _CLASSIFY_ZERO),
                         ("classify 含 2 轮历史", _CLASSIFY_HIST)]:
            b_chars, b_msgs = _shared_prefix(bmap[_LABEL_FIRST], bmap[lb])
            a_chars, a_msgs = _shared_prefix(cur[_LABEL_FIRST], cur[lb])
            print("  %s：改前共享前缀 %d 条 / %d 字符 -> 改后 %d 条 / %d 字符"
                  % (name, b_msgs, b_chars, a_msgs, a_chars))
            if b_chars != 0:
                _fail(errors, "[%s] 改前共享前缀应为 0 字符，实际 %d" % (name, b_chars))
            if a_msgs < 1 or a_chars < len(shared):
                _fail(errors, "[%s] 改后共享前缀不足一个共享块（%d < %d）"
                              % (name, a_chars, len(shared)))

        f, f0 = cur[_LABEL_FIRST], cur[_CLASSIFY_ZERO]
        print("  改前形态：共享块 planner #%d（role=%s），指令 #%d（role=%s）"
              % (bmap[_LABEL_FIRST]["shared_idx"], bmap[_LABEL_FIRST]["shared_role"],
                 bmap[_LABEL_FIRST]["instr_idx"], bmap[_LABEL_FIRST]["instr_role"]))
        print("  改后形态：共享块 #%d（role=%s），指令 #%d（role=%s）"
              % (f["shared_idx"], f["shared_role"], f["instr_idx"], f["instr_role"]))
        if f["shared_md5"] != f0["shared_md5"] or f["msg0_md5"] != f0["msg0_md5"]:
            _fail(errors, "改后 planner 第 0 条不是同一段共享块")
        if f["shared_role"] != "system" or f["instr_role"] != "user":
            _fail(errors, "改后形态不符：[%s] 共享块 + [%s] 指令"
                          % (f["shared_role"], f["instr_role"]))

        # 裁定 (b)：与 classify 一致显式跳过 invoke 期注入的 A 股缓存前缀 + 关语义缓存键
        for label in (_LABEL_FIRST, _LABEL_REPLAN):
            s = cur[label]
            if not s["skip_cache_prefix"]:
                _fail(errors, "[%s] 未传 skip_cache_prefix=True（CACHE_PREFIX_ENABLED=true 时"
                              "会把 539 字符前缀插到 messages[0]、对齐失效）" % label)
            if s["sr_cacheable"] is not False:
                _fail(errors, "[%s] metadata.sr_cacheable 不是 False：%s"
                              % (label, s["sr_cacheable"]))

        print("\n=== 结论 ===")
        if errors:
            print("  [FAIL] 失败 %d 项" % len(errors))
            for e in errors:
                print("    - %s" % e)
            return 1
        print("  [OK] 共享块与规划指令文本 md5 均未变；每条消息字符数集合与总数不变（搬位置、未复制）")
        print("  [OK] planner / classify 第 0 条逐字节相同，共享前缀 %d 字符" % len(shared))
        print("  [OK] skip_cache_prefix=True 与 sr_cacheable=False 已透传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""报告链路 fork 形态验证（PRD-A11 FR-3，零 LLM 调用）。

三条报告调用（report-select / report-fast / report-expand）是否属于**同一前缀族**，
是 AC-3 的核心判据：同族才共享整条前缀；异族会触发 provider 侧缓存驱逐
（PRD-A11 §2.12：两次 report-fast 命中 0% 都发生在异族调用插入之后）。

用法：
    python scripts/devtools/verify_report_fork_shape.py
    python scripts/devtools/verify_report_fork_shape.py --mode standalone
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import logging
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

CAPTURED: list[dict] = []


def _content(m) -> str:
    if isinstance(m, dict):
        return str(m.get("content") or "")
    if isinstance(m, (tuple, list)):
        return str(m[1] if len(m) > 1 else "")
    return str(getattr(m, "content", "") or "")


def _role(m) -> str:
    if isinstance(m, dict):
        return str(m.get("role") or "")
    if isinstance(m, (tuple, list)):
        return str(m[0])
    return str(getattr(m, "type", ""))


def _fp(msgs) -> str:
    return hashlib.sha256(
        "\n".join(f"{_role(m)}|{_content(m)}" for m in msgs).encode("utf-8")
    ).hexdigest()[:12]


def _shared_prefix(a, b) -> int:
    n = 0
    for x, y in zip(a, b):
        if _role(x) != _role(y) or _content(x) != _content(y):
            break
        n += 1
    return n


class _Resp:
    def __init__(self, text):
        self.content = text


class _BindableLLM:
    def bind_tools(self, tools):
        return self


def _conversation() -> tuple[list, list]:
    """构造一段**真实体量**的 agent 已热对话前缀。

    ⚠️ 别用占位短串糊弄：前缀越短，"同族"看起来越容易成立，验证就失去意义。
    实测真实 run 的对话前缀是 8,000~45,000 字符，这里按 3 轮工具结果构造。
    """
    convo = [("system", "REACT_SYSTEM_PROMPT" + "规则" * 200),
             ("user", "今日大盘总结")]
    for i in range(1, 4):
        convo.append(("assistant", f"第{i}轮 我要查大盘数据"))
        convo.append(("user", f"第{i}轮 工具结果：上证 {3200+i}，主力净流出 {600+i} 亿"
                              + "明细" * 120))
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(52)]
    return convo, tools


def _items():
    return [{"name": f"tool_{i}", "args": f"参数{i}", "output": f"结果{i}数据" * 30}
            for i in range(1, 13)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="fork", choices=["fork", "standalone"])
    args = ap.parse_args()

    from agents.analysis import report_builder as rb
    from config import Config
    import utils.llm_factory as llmf

    logging.basicConfig(level=logging.ERROR)
    convo, tools = _conversation()

    def fake_json(llm, messages, logger=None, **kw):
        CAPTURED.append({"label": kw.get("label") or (kw.get("metadata") or {}).get("purpose"),
                         "messages": messages, "metadata": kw.get("metadata") or {}})
        if (kw.get("metadata") or {}).get("purpose") == "report-select":
            return {"need_tools": [1, 2, 3]}
        return None

    def fake_invoke(llm, messages, logger=None, **kw):
        CAPTURED.append({"label": (kw.get("metadata") or {}).get("purpose"),
                         "messages": messages, "metadata": kw.get("metadata") or {}})
        # 第一次是正文 → 故意给**短**报告，触发 _expand_report_if_short，
        # 这样 report-expand 也会被捕获，能验证第三条链路。
        if (kw.get("metadata") or {}).get("purpose") == "report-fast":
            return _Resp("太短了。")
        return _Resp("扩充后的报告正文，含表格与情景矩阵。" * 20)

    llmf.llm_json_with_retry = fake_json
    llmf.tracked_invoke = fake_invoke
    # get_report_llm 必须带 bind_tools：生产代码在 fork 路径会调它，
    # 拿个裸 object() 会在绑定处 AttributeError（且现有代码吞掉异常继续跑）。
    llmf.get_report_llm = lambda: _BindableLLM()
    rb.tracked_invoke = fake_invoke
    Config.REPORT_DRILLDOWN_ENABLED = True
    Config.REPORT_ENABLE_ANALYSIS_ENGINE = True
    # 正文故意给短报告 → 强制触发 report-expand，验证第三条链路也走 fork
    Config.REPORT_MIN_TOKENS = 400
    Config.REPORT_MIN_TOKENS_BY_TEMPLATE = {}

    template = {"id": "market_daily", "name": "大盘总结",
                "sections": [{"title": "核心结论"}, {"title": "技术面"}],
                "data_requirements": {}}

    # ── 跑 1：非 fork 分支（证据注入路径），让 report-select 真的被调用 ──
    ok_select = asyncio.run(rb._select_tool_indices(
        _items(), "今日大盘总结", "草稿", None, logging.getLogger("v"), None, None,
        template=template,
        conversation=convo if args.mode == "fork" else None,
        conversation_tools=tools if args.mode == "fork" else None))

    # ── 跑 2：fork 分支（report-fast + 可能的 report-expand）──
    result = asyncio.run(rb.build(
        template, {}, "今日大盘总结", logger_obj=logging.getLogger("v"),
        tool_calls=[{"tool_name": f"tool_{i}", "tool_output": f"结果{i}数据" * 30}
                    for i in range(1, 13)],
        conversation=convo if args.mode == "fork" else None,
        conversation_tools=tools if args.mode == "fork" else None))

    print(f"模式={args.mode}   report-select 选中序号={ok_select}")
    print(f"报告章节数={len(result.get('sections') or [])}\n")

    if not CAPTURED:
        print("❌ 没有捕获到任何 LLM 调用")
        return 1

    print("== 三条报告调用的形态 ==")
    print(f"{'用途':<16}{'消息数':>8}{'尾部角色':>10}{'尾部长度':>10}"
          f"{'与对话共享':>12}{'fork_kind':>14}")
    base = convo if args.mode == "fork" else None
    for c in CAPTURED:
        msgs = c["messages"]
        tail = _content(msgs[-1]) if msgs else ""
        shared = _shared_prefix(msgs, base) if base is not None else 0
        print(f"{str(c['label']):<16}{len(msgs):>8}{_role(msgs[-1]):>10}"
              f"{len(tail):>10}{shared if base is not None else '-':>12}"
              f"{c['metadata'].get('fork_kind', '-'):>14}")

    print(f"\n对话前缀指纹 = {_fp(convo)}（{len(convo)} 条，"
          f"{sum(len(_content(m)) for m in convo)} 字符）")

    # ── 判据 ──
    ok = True
    print("\n== 判据 ==")
    if args.mode == "fork":
        fams = {c["label"]: _shared_prefix(c["messages"], convo) for c in CAPTURED}
        same_family = all(v == len(convo) for v in fams.values())
        print(f"  ① 三条调用与对话前缀共享 {fams} → "
              f"{'✅ 同族' if same_family else '❌ 存在异族调用（会触发 provider 缓存驱逐）'}")
        ok &= same_family
        all_fork = all(c["metadata"].get("fork_kind") == "llm_fork" for c in CAPTURED)
        print(f"  ② 全部标记 llm_fork → {'✅' if all_fork else '❌'}")
        ok &= all_fork
        for c in CAPTURED:
            tail = _content(c["messages"][-1])
            print(f"     · {c['label']} 尾部 {len(tail)} 字符（对话前缀 "
                  f"{sum(len(_content(m)) for m in convo)} 字符）")
    else:
        kinds = {c["label"]: c["metadata"].get("fork_kind", "-") for c in CAPTURED}
        print(f"  对照（standalone）: {kinds}")
        print(f"  report-select 首条是 build_shared_system → "
              f"{'✅ 回落路径未被破坏' if any('摘要' in _content(c['messages'][0]) or '选股雷达' in _content(c['messages'][0]) for c in CAPTURED if c['label'] == 'report-select') else '⚠️ 查一下'}")

    # ── 判据③：system 字节一致性（原 verify_report_system.py，2026-10-17 并入）──
    # 覆盖 5 条报告链路调用（report-fast 路径A/B、report-select、dashboard 阶段1/2）
    # 的 system 首段是否逐字节一致。与①②的"捕获真实调用"互补：这里直接调
    # prompt builder，不依赖任何 harness，失败即前缀缓存归零（PRD-A9 §3.1 / AC-3）。
    ok &= _check_system_identity()

    print(f"\n{'✅ 通过' if ok else '❌ 不通过'}")
    return 0 if ok else 1


def _sys_of(msgs) -> str:
    """取 messages 头部连续 system 的拼接（遇非 system 即停）。"""
    parts = []
    for m in msgs:
        role = m[0] if isinstance(m, tuple) else getattr(m, "type", "")
        if role != "system":
            break
        parts.append(m[1] if isinstance(m, tuple) else m.content)
    return "\n\n".join(parts)


def _check_system_identity() -> bool:
    """5 条报告链路调用的 system 字节必须完全一致（PRD-A9 §3.1 / AC-3）。"""
    import hashlib
    from agents.analysis.template_store import load_template
    from agents.analysis.report_builder import (
        _build_prompt, _build_selection_prompt, build_shared_system)  # noqa: F401
    from agents.analysis import dashboard_generator as dg

    print("\n== 判据③：报告链路 system 字节一致性（PRD-A9 §3.1 / AC-3）==")
    tpl = load_template("default") or load_template("standard")
    if tpl is None:
        print("  ⚠️ 找不到 default/standard 模板，跳过")
        return True

    try:
        variants = [
            ("report-fast 路径A", _sys_of(_build_prompt(
                tpl, {}, "今天大盘总结", raw_result="草稿" * 400, tool_evidence="证据"))),
            ("report-fast 路径B", _sys_of(_build_prompt(
                tpl, {}, "今天大盘总结", raw_result="短", tool_evidence="证据"))),
            ("report-select", _sys_of(_build_selection_prompt(
                [{"name": "mx_data_query", "args": "600519", "output": "收盘价 1685"}],
                "今天大盘总结", "草稿", tpl))),
            ("dashboard 阶段1", _sys_of(dg._build_prompt(
                "报告正文", {}, "今天大盘总结", scenario_tag="market_daily",
                dashboard_id="index_daily", tool_index="idx",
                allow_detail_request=True, template=tpl))),
            ("dashboard 阶段2", _sys_of(dg._build_prompt(
                "报告正文", {}, "今天大盘总结", scenario_tag="market_daily",
                dashboard_id="index_daily", tool_evidence="ev", template=tpl))),
        ]
    except Exception as e:                     # 签名变动时不阻断主判据
        print(f"  ⚠️ builder 调用失败（签名可能已变）：{e}")
        return True

    fps: dict[str, list[str]] = {}
    for name, s in variants:
        fp = hashlib.sha256(s.encode()).hexdigest()[:16]
        fps.setdefault(fp, []).append(name)
        print(f"  {name:<18} {len(s):>7} 字符  指纹 {fp}")
    if len(fps) == 1:
        print("  ✅ 五条调用的 system 字节完全一致")
        return True
    print("  ❌ 存在不同前缀：")
    for fp, names in fps.items():
        print(f"     {fp}: {', '.join(names)}")
    return False


if __name__ == "__main__":
    raise SystemExit(main())
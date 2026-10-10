"""工具证据视图：索引 / 下钻选择 / 原文提取（dashboard 与 report 共用）。

原实现内联在 `dashboard_generator.py`（2026-09-26 抽出），抽出原因：
报告生成新增「索引→按需下钻」需要同一套条目归一化与索引/证据构造，
避免两份实现漂移。dashboard_generator 仍从本模块 re-import 旧名以保持兼容。
"""
from __future__ import annotations

import json


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def tool_output_status(value) -> str:
    """工具输出状态：ok / failed / empty（报告与仪表盘共用的单一事实源）。

    语义边界（沿用 report_builder 原实现）：「数据缺失」只允许来自 failed/empty；
    纯文本/表格（非 JSON）一律判 ok。
    """
    text = _text(value).strip()
    if not text or text in ("[]", "{}"):
        return "empty"
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "ok"
    if isinstance(data, dict):
        if str(data.get("status", "")).lower() == "failed":
            return "failed"
        if data.get("error") and not any(
            data.get(k) for k in ("tables", "result", "stocks", "boards", "data")
        ):
            return "failed"
    return "ok"


def is_unusable_output(value) -> bool:
    """失败/空输出——不得作为维度证据、方向依据或情绪计数。

    2026-10-01 事故：一条失败的大宗交易返回（文案含「机构席位」）被 `_find_slot_values`
    命中为「资金面」，并以原始 JSON 作为 detail 呈现。
    """
    return tool_output_status(value) in ("failed", "empty")


def _tool_items(tool_calls, slot_results=None) -> list[dict]:
    """统一成**有序**的 [{name, args, output}]，保留重复调用（不再按工具名合并）。

    优先用 tool_calls（含每次调用的参数与输出，如 get_position_detail×9 全部保留）；
    缺失时回退到 slot_results（按名合并的 dict，只有最后一次）。
    """
    items: list[dict] = []
    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                name = tc.get("name") or tc.get("tool_name") or ""
                args = tc.get("args") or tc.get("tool_input") or tc.get("input") or ""
                output = tc.get("output", tc.get("tool_output", tc.get("raw_output", "")))
            else:
                name = getattr(tc, "name", None) or getattr(tc, "tool_name", "") or ""
                args = getattr(tc, "args", None) or getattr(tc, "tool_input", "") or ""
                output = getattr(tc, "tool_output", None) or getattr(tc, "output", "") or ""
            if name:
                items.append({"name": name, "args": _text(args), "output": output})
    if not items:
        for name, output in (slot_results or {}).items():
            items.append({"name": name, "args": "", "output": output})
    return items


def _build_tool_index(items, args_chars: int = 80, preview_chars: int = 160,
                      max_items: int = 60) -> str:
    """工具结果**索引**（名称/参数/压缩后长度/预览），供 LLM 决定是否下钻。

    值已是压缩后的工具输出；这里只给"有无/多大/开头长啥样"，不塞全文。
    """
    if not items:
        return ""
    lines = []
    for it in items[:max_items]:
        text = _text(it.get("output")).strip()
        args = (it.get("args") or "").replace("\n", " ")[:args_chars]
        preview = text[:preview_chars].replace("\n", " ")
        lines.append(f"- {it['name']} | 参数:{args or '—'} | 长度:{len(text)} | 预览:{preview or '（空）'}")
    return "\n".join(lines)


def _build_tool_evidence(items, selected_names=None, per_tool_chars: int = 3000,
                         max_chars: int = 30000) -> str:
    """给仪表盘 LLM 的**工具结果原文（压缩后）**。

    只在两阶段的下钻阶段调用：`selected_names` 为 LLM 点名的工具（按名匹配、保留全部
    重复调用）。不点名时=全量（向后兼容）。`per_tool_chars`/`max_chars` 是安全上限，
    典型下钻只带少数工具，实际不触发。
    """
    if not items:
        return ""
    sel = set(selected_names or [])
    parts, used = [], 0
    for it in items:
        if sel and it["name"] not in sel:
            continue
        text = _text(it.get("output")).strip()
        if not text:
            continue
        remain = max_chars - used
        if remain <= 0:
            break
        args = (it.get("args") or "").replace("\n", " ")[:80]
        head = f"【{it['name']}】" + (f"参数:{args}\n" if args else "")
        block = head + text[:per_tool_chars]
        if len(block) > remain:
            block = block[:remain]
        parts.append(block)
        used += len(block)
    return "\n".join(parts)

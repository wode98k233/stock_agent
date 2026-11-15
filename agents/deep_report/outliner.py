"""调度层：产出报告结构体（outline）。

M1 口径：
- 有 LLM → 让模型在大纲上做「本章重点」增强（不得增删章节 id / 不得改变顺序）；
- 无 LLM（离线/测试）→ 直接用模板结构（source='template'）；
- 解析失败 → 回落模板并记 raw_error（**不静默丢结构**）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agents.deep_report.models import Outline, OutlineSection
from agents.deep_report.templates import section_specs, total_words

logger = logging.getLogger(__name__)

_MIN_RATIO = 0.5   # LLM 返回章节数少于模板一半即判失败


def _to_outline(template: Dict[str, Any], specs: List[Dict[str, Any]], *,
                title: str, thesis: str, source: str, emphasis: Dict[str, str],
                cap_override: Optional[Dict[str, List[str]]] = None,
                raw_error: str = "") -> Outline:
    from agents.deep_report.capabilities import all_names, from_slot

    def caps_for(sid: str, slots: List[str]) -> List[str]:
        if cap_override and sid in cap_override:
            valid = [c for c in cap_override[sid] if c in all_names()]
            if valid:
                return valid
        out: List[str] = []
        for slot in slots:
            cap = from_slot(slot)
            if cap and cap not in out:
                out.append(cap)
        return out

    sections = [
        OutlineSection(
            seq=s["seq"], section_id=s["section_id"], title=s["title"],
            target_words=list(s["target_words"]), required=s["required"],
            data_slots=list(s["data_slots"]), capabilities=caps_for(s["section_id"], s["data_slots"]),
            charts=list(s["charts"]), supply=list(s["supply"]),
            emphasis=emphasis.get(s["section_id"], ""),
        )
        for s in specs
    ]
    return Outline(title=title, template_id=template["template_id"],
                   variant=template.get("variant", ""), thesis=thesis,
                   sections=sections, total_words=total_words(template),
                   source=source, raw_error=raw_error)


def _prompt(template: Dict[str, Any], specs: List[Dict[str, Any]],
            universe: str, user_request: str) -> str:
    from agents.deep_report.capabilities import describe_for_prompt
    lines = [
        "你是证券研究报告的主编。请为下面这份深度报告做「大纲增强」，只输出严格 JSON。",
        f"标的：{universe}",
        f"用户要求：{user_request or '（无）'}",
        f"模板：{template.get('name')}（变体 {template.get('variant')}）",
        "模板章节（id / 标题 / 目标字数）——**不得增删、不得改顺序、不得改 id**：",
    ]
    for s in specs:
        lines.append(f"- {s['section_id']} | {s['title']} | {s['target_words']}")
    lines += [
        "",
        "可用**能力**目录（只能从中选，不要写具体工具或数据厂商）：",
        describe_for_prompt(),
        "",
        "任务：",
        "1) 给出报告标题 title（不超过 24 字，必须体现标的与核心判断）；",
        "2) 给出一句话投资逻辑 thesis（不超过 60 字）；",
        "3) 为每个 section 写 emphasis（不超过 40 字，说明本章要回答什么）；",
        "4) 为每个 section 选 0–3 个 capabilities（本章需要哪些数据能力；宁缺毋滥，"
        "不需要数据的章节给空数组）；",
        "5) 若模板里某章对当前标的明显不适用，把该章 required 置 false（仍保留章节）。",
        '输出 JSON：{"title": "...", "thesis": "...", '
        '"sections": [{"section_id": "...", "emphasis": "...", "required": true, '
        '"capabilities": ["series_history"]}]}',
        "只输出 JSON，不要任何解释。",
    ]
    return "\n".join(lines)


def build_outline(template: Dict[str, Any], *, universe: str, user_request: str = "",
                  llm=None, log=None) -> Outline:
    specs = section_specs(template)
    fallback_title = f"{universe}深度报告"

    if llm is None:
        return _to_outline(template, specs, title=fallback_title, thesis="",
                           source="template", emphasis={})

    from utils.llm_factory import llm_json_with_retry
    messages = [
        ("system", "你是严谨的证券研究报告主编，只输出可解析的 JSON。"),
        ("user", _prompt(template, specs, universe, user_request)),
    ]
    data = llm_json_with_retry(llm, messages, log, label="deep-outline", max_retries=2)
    if not isinstance(data, dict):
        return _to_outline(template, specs, title=fallback_title, thesis="",
                           source="template", emphasis={}, raw_error="outline_json_invalid")

    # 校验：章节 id 必须与模板一致（顺序/数量不允许被模型改动）
    got = {str(s.get("section_id")): s for s in (data.get("sections") or []) if isinstance(s, dict)}
    known = {s["section_id"] for s in specs}
    missing = known - set(got)
    if len(got) < max(1, int(len(specs) * _MIN_RATIO)) or missing:
        return _to_outline(template, specs, title=fallback_title, thesis="",
                           source="template", emphasis={},
                           raw_error=f"outline_sections_mismatch missing={sorted(missing)[:3]}")

    emphasis: Dict[str, str] = {}
    required_override: Dict[str, bool] = {}
    cap_override: Dict[str, List[str]] = {}
    for sid, sec in got.items():
        if sid in known:
            emphasis[sid] = str(sec.get("emphasis") or "")[:120]
            if sec.get("required") is False:
                required_override[sid] = False
            caps = sec.get("capabilities")
            if isinstance(caps, list):
                cap_override[sid] = [str(c) for c in caps][:3]

    outline = _to_outline(
        template, specs,
        title=str(data.get("title") or fallback_title)[:40],
        thesis=str(data.get("thesis") or "")[:120],
        source="llm", emphasis=emphasis, cap_override=cap_override,
    )
    for s in outline.sections:
        if s.section_id in required_override:
            s.required = False
    return outline

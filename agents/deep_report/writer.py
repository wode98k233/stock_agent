"""分节写作（串行）。

三条硬约束（对应设计 v1.1）：
1. **禁止硬切**：`finish_reason == length` 时续写剩余部分，不截断字符串；
2. 「已写章节结论清单」只许 **尾部 append**（拼进 system/中段会让每节前缀归零）；
3. 数据缺失写「数据缺失：<字段>」，不许 0 填充、不许「约」。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from agents.deep_report.models import Outline, OutlineSection, SectionResult
from agents.deep_report.models import SEC_FAILED, SEC_OK

logger = logging.getLogger(__name__)

_CN_CHAR = re.compile(r"[\u4e00-\u9fff]")
_MAX_CONTINUE = 2


def count_words(text: str) -> int:
    """中文按字计，英文按词计（粗略但稳定，够做篇幅校验）。"""
    if not text:
        return 0
    cn = len(_CN_CHAR.findall(text))
    en = len(re.findall(r"[A-Za-z]+", text))
    return cn + en


def _finish_reason(resp: Any) -> Optional[str]:
    """尽最大努力取 finish_reason（不同 provider 放的位置不同）。"""
    for attr in ("finish_reason",):
        val = getattr(resp, attr, None)
        if val:
            return str(val)
    meta = getattr(resp, "response_metadata", None) or {}
    if isinstance(meta, dict):
        for key in ("finish_reason", "stop_reason"):
            if meta.get(key):
                return str(meta[key])
    extra = getattr(resp, "additional_kwargs", None) or {}
    if isinstance(extra, dict):
        for key in ("finish_reason", "stop_reason"):
            if extra.get(key):
                return str(extra[key])
    return None


def _system_prompt(template: Dict[str, Any]) -> str:
    rules = "\n".join(f"- {r}" for r in (template.get("qa_rules") or []))
    return (
        "你是资深证券分析师，正在撰写一份深度研究报告的**其中一章**。\n"
        "要求：\n"
        "- 只写本章，不要写目录、不要写其它章节、不要写免责声明；\n"
        "- 所有结论必须引用具体数字与事实，并标注（来源，日期）；\n"
        "- 没有数据就写「数据缺失：<字段>」，禁止编造、禁止用 0 或「约」替代；\n"
        "- **同一缺口只写一次，且每节最多 1 处「数据缺失」**——全文缺口统一由附录声明，"
        "正文不要逐项罗列缺失字段（罗列会让报告变成缺口清单）；\n"
        "- 禁止把「PE 分位低」写成「便宜」（金融行业以 PB-ROE 为主锚）；\n"
        "- 用 Markdown，二级标题以本章序号开头；表格不超过 5 行 × 6 列。\n"
        f"模板质量要求：\n{rules}"
    )


def _conclusions_of(prev: List[SectionResult]) -> str:
    if not prev:
        return "（本章是第一章，暂无已写章节）"
    lines = []
    for r in prev:
        if r.status != SEC_OK:
            continue
        snippet = re.sub(r"\s+", " ", r.content or "").strip()
        lines.append(f"- 第{r.seq}章「{r.title}」：{snippet[:160]}")
    return "\n".join(lines) if lines else "（暂无已写章节）"


def _user_prompt(outline: Outline, sec: OutlineSection, prev: List[SectionResult],
                 evidence: List[Dict[str, Any]]) -> str:
    lo, hi = (sec.target_words + [0, 0])[:2]
    lines = [
        f"报告标题：{outline.title}",
        f"全文投资逻辑（如已定）：{outline.thesis or '（未定）'}",
        f"章节：第 {sec.seq} 章「{sec.title}」（目标 {lo}–{hi} 字）",
    ]
    if sec.emphasis:
        lines.append(f"本章重点：{sec.emphasis}")
    if sec.data_slots:
        lines.append(f"可用数据槽：{', '.join(sec.data_slots)}（M1 尚未接入取数，缺数据请显式标注）")
    if evidence:
        from agents.deep_report.fetch import summary_lines
        lines.append("## 本节可用证据（只准引用这里出现过的数字，并带来源与日期）")
        gaps: List[str] = []
        for card in evidence:
            # 逐股横截面卡片（peer_*）需要更多行：默认 6 行会被截成“只有几只股票”
            lines.extend(summary_lines(card, limit=int(card.get("inject_limit") or 6)))
            gaps.extend(str(g) for g in (card.get("gaps") or []))
        if gaps:
            lines.append("## 缺口（必须写「数据缺失：<字段>」）")
            for g in gaps:
                lines.append(f"- {g}")
    lines.append("")
    lines.append("已写章节结论清单（用于衔接，禁止重复其内容）：")
    lines.append(_conclusions_of(prev))
    lines.append("")
    lines.append(f"请输出第 {sec.seq} 章正文。")
    return "\n".join(lines)


def _stub_section(sec: OutlineSection, universe: str) -> str:
    lo, hi = (sec.target_words + [0, 0])[:2]
    return (
        f"## {sec.seq}. {sec.title}\n\n"
        f"（M1 离线占位：本章「{sec.title}」尚未接入取数与写作模型，目标 {lo}–{hi} 字。）\n\n"
        f"- 标的：{universe}\n"
        f"- 数据缺失：本节数据槽未接入（{', '.join(sec.data_slots) or '无'}）\n"
        f"- 影响：本章结论暂不成立，仅用于验证编排与版式\n"
    )


def write_section(outline: Outline, sec: OutlineSection, *, template: Dict[str, Any],
                  prev: List[SectionResult], evidence: Optional[List[Dict[str, Any]]] = None,
                  universe: str = "", llm=None, log=None, cancel_check=None,
                  revision: Optional[Dict[str, str]] = None,
                  previous: str = "") -> SectionResult:
    if llm is None:
        body = previous or _stub_section(sec, universe)
        return SectionResult(sec.seq, sec.section_id, sec.title, SEC_OK,
                             content=body, words=count_words(body))

    try:
        from utils.llm_factory import tracked_invoke
        if revision:
            messages = [
                ("system", _system_prompt(template)),
                ("user", _revision_prompt(outline, sec, revision, previous)),
            ]
            label = f"deep-revise-{sec.seq}"
        else:
            messages = [
                ("system", _system_prompt(template)),
                ("user", _user_prompt(outline, sec, prev, evidence or [])),
            ]
            label = f"deep-write-{sec.seq}"
        resp = tracked_invoke(llm, messages, log, label=label)
        content = getattr(resp, "content", None) or str(resp)
        continued = 0

        # 禁止硬切：输出被截断 → 续写，而不是截断字符串
        while _finish_reason(resp) in ("length", "max_tokens", "max-tokens") and continued < _MAX_CONTINUE:
            if cancel_check and cancel_check():
                break
            continued += 1
            tail = "\n".join([
                "你上一段输出被长度限制截断了。请从截断处**接着写**，不要重复已写内容，不要重写标题。",
                "已写内容（末尾 400 字）：",
                (content or "")[-400:],
            ])
            resp = tracked_invoke(llm, messages + [("assistant", content), ("user", tail)],
                                  log, label=f"{label}-cont{continued}")
            more = getattr(resp, "content", None) or str(resp or "")
            content = (content or "") + "\n" + more

        words = count_words(content)
        return SectionResult(sec.seq, sec.section_id, sec.title, SEC_OK,
                             content=content, words=words, continued=continued)
    except Exception as e:  # noqa: BLE001 —— 单节失败不能炸整份报告
        logger.warning("章节写作失败 seq=%s: %s", sec.seq, e)
        return SectionResult(sec.seq, sec.section_id, sec.title, SEC_FAILED,
                             content="", error=f"{type(e).__name__}: {e}")


def _revision_prompt(outline: Outline, sec: OutlineSection,
                     revision: Dict[str, str], previous: str) -> str:
    lo, hi = (sec.target_words + [0, 0])[:2]
    return "\n".join([
        f"报告标题：{outline.title}",
        f"章节：第 {sec.seq} 章「{sec.title}」（原长 {count_words(previous)} 字，目标 {lo}–{hi} 字）",
        "",
        "## 必须修正",
        revision.get("fix", ""),
        "",
        "## 必须保持",
        revision.get("keep", "其余段落、结构与结论保持不变"),
        "- 不得新增原正文没有的数字；需要新数据时写「数据缺失：<字段>」",
        "- 字数与原章相差不超过 20%",
        "",
        "## 原正文",
        previous,
        "",
        "请输出改写后的**整节正文**，不要解释、不要重复标题。",
    ])

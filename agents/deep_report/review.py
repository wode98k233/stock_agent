"""调度 agent 的「审稿」：读摘要 + 冲突清单，决定改哪几节、按什么改。

关键口径：**它不读全文**。输入是每节 3–5 条结论 + 程序校验的冲突清单，
输出是「返工指令」，真正改稿由写作节单节重跑完成（见 runtime）。

无 LLM 时走规则兜底：只要冲突可定位到章节，就按证据卡值生成返工指令。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_REWRITES = 1   # 返工是乘法成本：一次只改一节（与 runtime 的评审预算配合）
_CLAIM_MAX = 5


def claims_of(section: Any, limit: int = _CLAIM_MAX) -> List[str]:
    """从章节正文里抽结论清单（首句 + 列表项），给调度做上下文，不送全文。"""
    text = (getattr(section, "content", "") or "").strip()
    if not text:
        return []
    out: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("|") or s.startswith("!"):
            continue
        s = s.lstrip("-*0123456789. ").strip()
        if len(s) < 8:
            continue
        out.append(s[:120])
        if len(out) >= limit:
            break
    return out


def _rule_rewrites(qa: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_seq: Dict[Any, List[Dict[str, Any]]] = {}
    for c in qa.get("conflicts") or []:
        for seen in c.get("seen") or []:
            seq = seen.get("seq")
            if seq is None:
                continue
            by_seq.setdefault(seq, []).append(c)
    out: List[Dict[str, Any]] = []
    for seq, items in by_seq.items():
        facts = "；".join(
            f"{c['metric']} 应为 {c['expected']}{c.get('unit') or ''}"
            f"（{c.get('ev_id')}，{c.get('asof')}）" for c in items[:3])
        out.append({"seq": seq,
                    "fix": f"正文中的数字与证据卡不一致，必须改为：{facts}",
                    "keep": "其余段落、结构与结论保持不变"})
        if len(out) >= MAX_REWRITES:
            break
    return out


def _prompt(outline: Any, sections: List[Any], qa: Dict[str, Any], semantic: bool) -> str:
    payload = {
        "title": getattr(outline, "title", ""),
        "thesis": getattr(outline, "thesis", ""),
        "sections": [{"seq": getattr(s, "seq", None), "title": getattr(s, "title", ""),
                      "words": getattr(s, "words", None), "claims": claims_of(s)}
                     for s in sections],
        "conflicts": qa.get("conflicts") or [],
        "checks": [{"id": c["id"], "passed": c["passed"], "detail": c["detail"]}
                   for c in qa.get("checks") or []],
    }
    return (
        "你是这份深度报告的主编，现在做终审。你**看不到全文**，只看投资逻辑、"
        "每节的结论摘要与程序校验结果。请判断两件事：\n"
        "A) 事实冲突：哪些章节的数字与证据卡不一致；\n"
        "B) **语义一致性**：章节结论之间、以及章节与「投资逻辑 thesis」之间是否互相矛盾"
        "（典型：thesis 说看好，摘要却说中性；前一章说估值高位、后一章说估值洼地）。\n"
        "规则：\n"
        f"1) 最多指定 {MAX_REWRITES} 节返工，能不返工就不返工；\n"
        "2) 返工理由必须指向具体事实或具体矛盾（引用哪一节哪句话），不得说『写得更专业一点』；\n"
        "3) 若章节结论与 thesis 矛盾且章节有证据支撑，应优先建议**改 thesis**，"
        "在 thesis_fix 里给出一句新的 thesis；若标题（title）与正文矛盾或已不适用，"
        "在 title_fix 里给出新标题（≤24 字，必须体现标的与核心判断）；\n"
        "4) 无问题时 verdict=ok、rewrites 为空。\n"
        "只输出 JSON：{\"verdict\":\"ok|revise\",\"reason\":\"...\","
        "\"semantic_issues\":[\"第3章说X，第6章说Y\"],\"thesis_fix\":\"\",\"title_fix\":\"\","
        "\"rewrites\":[{\"seq\":1,\"fix\":\"...\",\"keep\":\"...\"}]}\n\n"
        "输入：\n" + json.dumps(payload, ensure_ascii=False)
    )


def review(outline: Any, sections: List[Any], qa: Dict[str, Any],
           *, llm=None, log=None, semantic: bool = True) -> Dict[str, Any]:
    # 程序校验全过时：仍有语义检查需求（thesis 与章节结论可能互斥），所以不能直接返回 ok
    if qa.get("passed") and not semantic:
        return {"verdict": "ok", "reason": "程序校验全部通过（跳过语义审稿）",
                "rewrites": [], "semantic_issues": [], "thesis_fix": "", "source": "rule"}

    rewrites = _rule_rewrites(qa)
    if llm is None:
        return {"verdict": "revise" if rewrites else "ok",
                "reason": "无 LLM，按证据卡生成规则返工" if rewrites else "冲突无法定位章节",
                "rewrites": rewrites, "semantic_issues": [], "thesis_fix": "", "source": "rule"}

    try:
        from utils.llm_factory import llm_json_with_retry
        data = llm_json_with_retry(
            llm,
            [("system", "你是严谨的证券研究报告主编，只输出 JSON。"),
             ("user", _prompt(outline, sections, qa, semantic))],
            log or logger, label="deep-review", max_retries=2)
    except Exception as e:  # noqa: BLE001
        logger.warning("调度审稿失败，回落规则返工: %s", e)
        return {"verdict": "revise" if rewrites else "ok", "reason": f"LLM 失败: {e}",
                "rewrites": rewrites, "semantic_issues": [], "thesis_fix": "", "source": "rule"}

    if not isinstance(data, dict):
        return {"verdict": "revise" if rewrites else "ok", "reason": "审稿输出非法 JSON",
                "rewrites": rewrites, "semantic_issues": [], "thesis_fix": "", "source": "rule"}

    valid_seqs = {getattr(s, "seq", None) for s in sections}
    clean: List[Dict[str, Any]] = []
    for r in (data.get("rewrites") or [])[:MAX_REWRITES]:
        if not isinstance(r, dict):
            continue
        seq = r.get("seq")
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            continue
        if seq not in valid_seqs or not str(r.get("fix") or "").strip():
            continue
        clean.append({"seq": seq, "fix": str(r["fix"])[:400],
                      "keep": str(r.get("keep") or "其余内容保持不变")[:200]})
    verdict = str(data.get("verdict") or ("revise" if clean else "ok"))
    issues = [str(x)[:200] for x in (data.get("semantic_issues") or []) if str(x).strip()][:5]
    return {"verdict": verdict, "reason": str(data.get("reason") or "")[:300],
            "rewrites": clean, "semantic_issues": issues,
            "thesis_fix": str(data.get("thesis_fix") or "").strip()[:200],
            "title_fix": str(data.get("title_fix") or "").strip()[:40],
            "source": "llm"}

"""证据引用强制校验：findings 的 quote 必须能在证据里原文命中。

这是治「编造痛点」的关键机制 —— judge 想糊弄就必须引用真实存在的原文。
校验是**纯函数**（不调 LLM），便于单测。

降级语义：校验不过的 finding **保留但标 `unverified`**，不静默丢弃 ——
丢弃会让「judge 在糊弄」变得不可见。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from eval.evidence.extract import contains_quote
from eval.evidence.trace_source import RunEvidence, load_run_evidence, text_at_step

# 证据来源 → 解析文本的键
SOURCE_TOOL_OUTPUT = "tool_output"
SOURCE_ANSWER = "answer"
SOURCE_USER_MESSAGE = "user_message"
SOURCE_THINK = "think_units"
KNOWN_SOURCES: tuple[str, ...] = (
    SOURCE_TOOL_OUTPUT, SOURCE_ANSWER, SOURCE_USER_MESSAGE, SOURCE_THINK,
)

# (source, ref) -> 原文；ref 为 step_ref 或 turn_index，无法定位时传 None
SourceResolver = Callable[[str, "int | None"], str]


@dataclass
class ValidationOutcome:
    findings: list[dict]
    unverified_count: int
    verified_count: int
    failures: list[dict] = field(default_factory=list)   # 未命中的引用（供带反馈重试）
    unknown_dimensions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.unknown_dimensions

    @property
    def total(self) -> int:
        return self.verified_count + self.unverified_count


def make_run_resolver(conn: sqlite3.Connection, run_id: str,
                      evidence: RunEvidence | None = None) -> SourceResolver:
    """run 级证据解析器：把 (source, step_ref) 映射到原文。"""
    ev = evidence if evidence is not None else load_run_evidence(conn, run_id)

    def resolve(source: str, ref: int | None) -> str:
        if source == SOURCE_ANSWER:
            return ev.final_answer
        if source == SOURCE_USER_MESSAGE:
            return ev.question
        if source in (SOURCE_TOOL_OUTPUT, SOURCE_THINK):
            if ref is None:
                return ev.tool_outputs_text if source == SOURCE_TOOL_OUTPUT \
                    else "\n".join(ev.think_reasoning)
            return text_at_step(conn, run_id, ref)
        return ""

    return resolve


def make_dialog_resolver(turns: Iterable[dict],
                         run_resolver: SourceResolver | None = None) -> SourceResolver:
    """dialog 级解析器：按 turn_index 定位该轮的问题/回答；其余委托 run 级解析器。"""
    items = list(turns)

    def _turn(ref: int | None) -> dict | None:
        if ref is None:
            return None
        for t in items:
            if t.get("turn_index") == ref:
                return t
        return None

    def resolve(source: str, ref: int | None) -> str:
        if source == SOURCE_USER_MESSAGE:
            t = _turn(ref)
            return (t or {}).get("question") or ""
        if source == SOURCE_ANSWER:
            t = _turn(ref)
            return (t or {}).get("answer") or ""
        if run_resolver is not None:
            return run_resolver(source, ref)
        return ""

    return resolve


def _as_str_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []


def validate_findings(findings: Iterable[dict], resolver: SourceResolver,
                      *, valid_dimensions: Iterable[str] | None = None
                      ) -> ValidationOutcome:
    """逐条校验 findings 的 evidence 引用。

    判定规则：
    - 某条 finding 的**任一** evidence 命中 → 该 finding 视为已核验
    - 全部未命中 / 未提供 evidence → 标 `unverified: true` + 原因
    - dimension 不在注册表内 → 记入 `unknown_dimensions`
    """
    allowed = set(valid_dimensions) if valid_dimensions is not None else None
    out: list[dict] = []
    failures: list[dict] = []
    unknown_dims: list[str] = []
    verified = unverified = 0

    for raw in findings or []:
        if not isinstance(raw, dict):
            continue
        f = dict(raw)
        dim = str(f.get("dimension") or "")
        if allowed is not None and dim not in allowed and dim not in unknown_dims:
            unknown_dims.append(dim)

        ev_items = [e for e in (f.get("evidence") or []) if isinstance(e, dict)]
        checked: list[dict] = []
        any_hit = False
        for e in ev_items:
            source = str(e.get("source") or "")
            ref = e.get("step_ref", e.get("turn_index"))
            try:
                ref = int(ref) if ref is not None else None
            except (TypeError, ValueError):
                ref = None
            quote = str(e.get("quote") or "")
            text = resolver(source, ref) if source in KNOWN_SOURCES else ""
            hit = contains_quote(text, quote)
            any_hit = any_hit or hit
            checked.append({**e, "source": source, "step_ref": ref,
                            "quote": quote, "verified": hit})
            if not hit:
                failures.append({"dimension": dim, "source": source,
                                 "ref": ref, "quote": quote})

        f["evidence"] = checked
        if not ev_items:
            f["unverified"] = True
            f["unverified_reason"] = "未提供证据引用（findings 契约要求必须带 evidence）"
            unverified += 1
        elif any_hit:
            f["unverified"] = False
            f.pop("unverified_reason", None)
            verified += 1
        else:
            f["unverified"] = True
            f["unverified_reason"] = "引用的原文未能在证据中命中（归一化后仍不匹配）"
            unverified += 1
        out.append(f)

    return ValidationOutcome(findings=out, unverified_count=unverified,
                             verified_count=verified, failures=failures,
                             unknown_dimensions=unknown_dims)


def build_retry_feedback(outcome: ValidationOutcome) -> str:
    """生成带反馈重试的提示：把未命中的引用原样回给 judge。"""
    lines = ["以下引用无法在给定数据中找到，请重新引用真实存在的原文"
             "（不要改写、不要拼接），或删除该 finding："]
    for f in outcome.failures[:10]:
        loc = f"step_ref {f['ref']}" if f.get("ref") is not None else "无定位"
        lines.append(f"- [{f['dimension']}] {f['source']} · {loc} → 「{f['quote'][:80]}」")
    if outcome.unknown_dimensions:
        lines.append(f"另外，以下 dimension 不在允许清单内，请改用清单中的键："
                     f"{outcome.unknown_dimensions}")
    return "\n".join(lines)


def mark_low_confidence(scores: dict, findings: Iterable[dict],
                        *, threshold: float = 90.0) -> dict[str, bool]:
    """维度分低于 threshold 且**没有任何已核验 finding** → 标低置信。

    含义：这个维度扣了分，但扣分依据无法核验，分数不可尽信。
    """
    verified_dims = {str(f.get("dimension") or "")
                     for f in (findings or [])
                     if isinstance(f, dict) and not f.get("unverified")}
    result: dict[str, bool] = {}
    for dim, score in (scores or {}).items():
        try:
            s = float(score)
        except (TypeError, ValueError):
            s = 0.0
        result[dim] = s < threshold and dim not in verified_dims
    return result

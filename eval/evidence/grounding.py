"""规则取证：数字有无出处、数据是否新鲜。

产出的是**待复核对象**，不是定罪：模型自己的算术推导（如「占比 30%」由两个数算出）
无法自动判定，所以落在 `unverified` 里交给 judge 复核，不直接扣分。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from eval.evidence.extract import (
    NumberToken,
    extract_dates,
    extract_numbers,
    numbers_match,
)
from eval.evidence.trace_source import RunEvidence, load_run_evidence

# 源侧数字上限：防止超大 trace 拖慢核验；超限时报告 truncated 供上层降权
MAX_SOURCE_NUMBERS = 50_000

# 数据新鲜度阈值（天）：沿用项目 stale_threshold_days 的分档口径
FRESHNESS_THRESHOLDS = {"daily": 10, "monthly": 45, "quarterly": 135}
DEFAULT_FRESHNESS_BUCKET = "daily"


@dataclass(frozen=True)
class NumberClaim:
    """回答里的一个数字，以及它能否在来源中找到出处。"""

    raw: str
    value: float
    unit: str
    context: str
    supported: bool
    matched_step_refs: tuple[int, ...]   # 命中的工具步；空 = 只在用户问题里出现或没找到

    def to_dict(self) -> dict:
        return {"raw": self.raw, "value": self.value, "unit": self.unit,
                "context": self.context, "supported": self.supported,
                "matched_step_refs": list(self.matched_step_refs)}


@dataclass(frozen=True)
class GroundingReport:
    total: int
    supported: tuple[NumberClaim, ...]
    unverified: tuple[NumberClaim, ...]
    rate: float
    source_truncated: bool

    @property
    def has_issue(self) -> bool:
        return bool(self.unverified)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "supported_count": len(self.supported),
            "unverified_count": len(self.unverified),
            "rate": self.rate,
            "source_truncated": self.source_truncated,
            "supported": [c.to_dict() for c in self.supported],
            "unverified": [c.to_dict() for c in self.unverified],
        }


def _source_index(evidence: RunEvidence) -> tuple[list[tuple[NumberToken, int | None]], bool]:
    """来源池 = 全部工具返回 ∪ 用户问题。

    用户问题里的数字（refs=None）是**合法来源**：用户自己在提问里给的数字不是幻觉。
    """
    idx: list[tuple[NumberToken, int | None]] = []
    truncated = False
    for tool in evidence.tools:
        if not tool.output:
            continue
        for tok in extract_numbers(tool.output):
            if len(idx) >= MAX_SOURCE_NUMBERS:
                truncated = True
                break
            idx.append((tok, tool.step_ref))
        if truncated:
            break
    for tok in extract_numbers(evidence.question):
        idx.append((tok, None))
    return idx, truncated


def verify_numbers(conn: sqlite3.Connection, run_id: str,
                   answer: str | None = None,
                   *, rel_tol: float = 0.01) -> GroundingReport:
    """核验回答里的数字能否在工具返回 / 用户问题中找到出处。

    `answer` 省略时取 run 的最终回答。
    """
    evidence = load_run_evidence(conn, run_id)
    text = evidence.final_answer if answer is None else answer
    claims = extract_numbers(text)
    if not claims:
        return GroundingReport(0, (), (), 1.0, False)

    idx, truncated = _source_index(evidence)
    supported: list[NumberClaim] = []
    unverified: list[NumberClaim] = []

    for claim in claims:
        refs: list[int] = []
        whitelisted = False
        for src, ref in idx:
            if not numbers_match(claim, src, rel_tol):
                continue
            if ref is None:
                whitelisted = True          # 命中用户问题 → 合法来源，不算幻觉
            elif ref not in refs:
                refs.append(ref)
        ok = whitelisted or bool(refs)
        rec = NumberClaim(raw=claim.raw, value=claim.value, unit=claim.unit,
                          context=claim.context, supported=ok,
                          matched_step_refs=tuple(sorted(refs)))
        (supported if ok else unverified).append(rec)

    return GroundingReport(
        total=len(claims),
        supported=tuple(supported),
        unverified=tuple(unverified),
        rate=round(len(supported) / len(claims), 4),
        source_truncated=truncated,
    )


# ── 数据新鲜度 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FreshnessReport:
    run_date: str | None
    latest_data_date: str | None
    lag_days: int | None
    threshold_days: int
    stale: bool
    source_step_refs: tuple[int, ...]

    def to_dict(self) -> dict:
        return {"run_date": self.run_date, "latest_data_date": self.latest_data_date,
                "lag_days": self.lag_days, "threshold_days": self.threshold_days,
                "stale": self.stale,
                "source_step_refs": list(self.source_step_refs)}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s).date()      # 覆盖 2026-09-18 / 2026-09-18T10:00:00
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def check_freshness(evidence: RunEvidence, *,
                    bucket: str = DEFAULT_FRESHNESS_BUCKET,
                    threshold_days: int | None = None) -> FreshnessReport:
    """工具返回的数据日期相对 **run 执行时刻** 是否过期。

    参照点是 run 的 created_at 而非「今天」：存量回测要判断的是**当时**数据新不新鲜，
    用今天的日期去比会把所有历史 run 都判成过期。
    """
    thr = threshold_days if threshold_days is not None else \
        FRESHNESS_THRESHOLDS.get(bucket, FRESHNESS_THRESHOLDS[DEFAULT_FRESHNESS_BUCKET])

    run_date = _parse_date(evidence.created_at)
    latest: str | None = None
    refs: list[int] = []
    for tool in evidence.tools:
        if not tool.output:
            continue
        dates = extract_dates(tool.output)
        if not dates:
            continue
        tool_latest = max(dates)
        if latest is None or tool_latest > latest:
            latest = tool_latest
        if tool.step_ref not in refs:
            refs.append(tool.step_ref)

    if latest is None or run_date is None:
        return FreshnessReport(
            run_date=run_date.isoformat() if run_date else None,
            latest_data_date=latest, lag_days=None, threshold_days=thr,
            stale=False, source_step_refs=tuple(sorted(refs)))

    data_date = _parse_date(latest)
    lag = (run_date - data_date).days if data_date else None
    return FreshnessReport(
        run_date=run_date.isoformat(),
        latest_data_date=latest,
        lag_days=lag,
        threshold_days=thr,
        stale=bool(lag is not None and lag > thr),
        source_step_refs=tuple(sorted(refs)),
    )

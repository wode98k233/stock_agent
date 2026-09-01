"""
分析框架引擎 — 核心数据模型

5 个核心 dataclass + dict 约定的 EvidenceBag/EvidenceItem（第一期）
"""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AnalysisRequest:
    """分析请求"""
    user_input: str
    agent_name: str
    logger: Any
    memory: Any = None
    budget: Any = None
    template_id: Optional[str] = None
    horizon: Optional[str] = None
    report_mode: str = "fast"
    selected_skills: list = field(default_factory=list)


@dataclass
class SlotResult:
    """数据插槽提取结果"""
    slot: str
    value: Any
    status: str
    interpretation: str
    source_tool: Optional[str]
    method: str  # "json" | "table" | "regex" | "llm" | "derived"
    confidence: float
    evidence_refs: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class SectionPlan:
    """板块合成计划"""
    id: str
    title: str
    prompt: str
    status: str  # "ok" | "partial" | "degraded" | "skip"
    slot_results: dict = field(default_factory=dict)  # {slot_name: SlotResult}
    max_words: Optional[int] = None


@dataclass
class ReportSection:
    """报告板块"""
    id: str
    title: str
    content: str
    status: str  # "ok" | "partial" | "degraded" | "skip"
    evidence_refs: list = field(default_factory=list)


@dataclass
class AnalysisResult:
    """分析结果"""
    content: str
    report: Optional[dict] = None
    used_template: str = ""
    degraded: bool = False
    fallback_used: bool = False
    diagnostics: dict = field(default_factory=dict)
    dashboard: Optional[dict] = None


# ── dict 约定（第一期不升级为 dataclass）──

# EvidenceItem = {
#     "tool_name": str,
#     "tool_input": str,
#     "raw_output": str,
#     "output_preview": str,
#     "output_length": int,
#     "parsed_json": Any | None,
#     "tables": list[Any],
#     "created_at": str | None,
#     "truncated": bool,
# }

# EvidenceBag = {
#     "items": list[dict],          # EvidenceItem dict 列表
#     "raw_text": str,
#     "news_text": str,
#     "json_fragments": list[Any],
#     "normalized_fields": dict[str, Any],
#     "table_fragments": list[Any],
#     "metadata": dict[str, Any],
# }

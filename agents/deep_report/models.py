"""长报告数据结构（纯数据，无 IO）。

口径：
- 章节数由模板决定（C9），这里不写死 10；
- 缺数据一律 None / 空列表，**不用 0 填充**（与 thesis/context_stats 同源铁律）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# 报告级状态（与设计 §5.2 一致）
STATUS_DRAFT = "draft"
STATUS_OUTLINING = "outlining"
STATUS_WRITING = "writing"
STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 章节级状态
SEC_PENDING = "pending"
SEC_WRITING = "writing"
SEC_OK = "ok"
SEC_FAILED = "failed"
SEC_SKIPPED = "skipped"


@dataclass
class OutlineSection:
    seq: int
    section_id: str
    title: str
    target_words: List[int] = field(default_factory=lambda: [0, 0])
    required: bool = True
    data_slots: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)   # 调度分配的「能力」，不是厂商工具
    charts: List[str] = field(default_factory=list)
    supply: List[str] = field(default_factory=list)
    emphasis: str = ""          # 调度 agent 为本章追加的重点
    status: str = SEC_PENDING
    actual_words: Optional[int] = None
    error: str = ""

    @property
    def max_words(self) -> int:
        return int(self.target_words[1]) if len(self.target_words) > 1 else 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Outline:
    title: str
    template_id: str
    variant: str
    sections: List[OutlineSection]
    thesis: str = ""            # 一句话投资逻辑（摘要章会用到）
    total_words: List[int] = field(default_factory=lambda: [0, 0])
    source: str = "template"    # template（LLM 失败回落）/ llm
    raw_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "template_id": self.template_id,
            "variant": self.variant,
            "thesis": self.thesis,
            "total_words": self.total_words,
            "source": self.source,
            "raw_error": self.raw_error,
            "sections": [s.to_dict() for s in self.sections],
        }


@dataclass
class SectionResult:
    seq: int
    section_id: str
    title: str
    status: str
    content: str = ""
    words: Optional[int] = None
    error: str = ""
    continued: int = 0          # finish_reason=length 的续写次数

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq, "section_id": self.section_id, "title": self.title,
            "status": self.status, "words": self.words, "error": self.error,
            "continued": self.continued,
        }


@dataclass
class ReportMeta:
    rid: str
    universe: str
    user_request: str
    template_id: str
    variant: str
    dialog_uuid: str = ""      # 从对话发起时绑定的对话 id（用于卡片回溯）
    title: str = ""
    status: str = STATUS_DRAFT
    stage: str = ""
    progress: int = 0
    sections_total: int = 0
    sections_done: int = 0
    words: Optional[int] = None
    charts: Optional[int] = None
    qa_json: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

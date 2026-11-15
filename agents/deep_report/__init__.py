"""长报告（深度研报）骨架 —— M1：大纲 → 分节 → 汇编 → 静态阅读页。

设计：docs/arch/2026-10-31-long-report-agent-system-design.md（v1.1）
边界（M1 不做）：子代理取数、SSE/HTTP、SQLite、图表真实数据、统稿校验。
"""
from agents.deep_report.models import (  # noqa: F401
    Outline,
    OutlineSection,
    ReportMeta,
    SectionResult,
)
from agents.deep_report.runtime import DeepReportRuntime  # noqa: F401

__all__ = [
    "Outline", "OutlineSection", "ReportMeta", "SectionResult", "DeepReportRuntime",
]

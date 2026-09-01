"""
分析框架引擎 — Agent 后处理与报告合成层

提供统一 API: run_analysis()
任何 Agent 都可在工具调用完成后调用，将原始输出转换为结构化专业报告。
"""
from agents.analysis.engine import run_analysis
from agents.analysis.models import (
    AnalysisRequest, SlotResult, SectionPlan, ReportSection, AnalysisResult,
)

__all__ = [
    "run_analysis",
    "AnalysisRequest", "SlotResult", "SectionPlan", "ReportSection", "AnalysisResult",
]

from typing import Literal, Optional

from pydantic import BaseModel, Field


class DashboardCheckItem(BaseModel):
    """仪表盘检查项"""

    dimension: str
    status: Literal["positive", "warning", "negative"]
    detail: str


class DashboardRiskItem(BaseModel):
    """仪表盘风险项"""

    level: Literal["high", "medium", "low"]
    category: str
    detail: str
    action: str | None = None


class DashboardIndexItem(BaseModel):
    """指数数据项"""
    name: str
    close: float | None = None
    change_pct: float | None = None


class DashboardCandidateStock(BaseModel):
    """筛选候选标的"""
    rank: int
    name: str
    ticker: str = ""
    reason: str = ""          # 排序依据
    strength: str = ""        # 关键优势
    weakness: str = ""        # 主要短板
    suitability: str = ""     # 适合投资者类型


class DashboardData(BaseModel):
    """仪表盘数据模型"""

    core_verdict: str
    decision_type: Literal["buy", "hold", "sell"]
    confidence_level: float = Field(ge=0, le=1)
    sentiment_score: int = Field(ge=0, le=100)
    trend_prediction: Literal["bullish", "neutral", "bearish"]
    quality_tag: Literal["完整分析", "快速概览"] = "完整分析"
    checklist: list[DashboardCheckItem] = []
    risk_priority: list[DashboardRiskItem] = []
    key_points: list[str] = []
    next_watch: list[str] = []
    # ── 个股类 ──
    price_levels: dict | None = None
    split_advice: dict | None = None
    position_guidance: dict | None = None
    falsification_signal: str | None = None
    # ── 筛选类 ──
    candidate_stocks: list[DashboardCandidateStock] | None = None
    # ── 市场/板块公共 ──
    market_temperature: int | None = None
    sector_rotation: list | None = None
    sector_stage: str | None = None
    leading_stocks: list | None = None
    portfolio_action: str | None = None
    action_items: list | None = None
    event_impact: str | None = None
    # ── 市场类专用 ──
    index_data: list[DashboardIndexItem] | None = None
    market_breadth: dict | None = None
    strong_sectors: list[str] | None = None
    weak_sectors: list[str] | None = None
    volume_summary: str | None = None
    # ── 热点事件 ──
    lifecycle_stage: str | None = None
    sentiment_heat: int | None = None
    capital_behavior: str | None = None
    hot_stocks: list | None = None
    # ── 元数据 ──
    scenario_tag: str = "决策仪表盘"
    dashboard_id: str = ""

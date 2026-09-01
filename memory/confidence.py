"""
记忆系统 — 置信度评分（v2 M1）

4 因子合成模型：
  final_confidence = clamp(base × freshness × feedback × corroboration, 0, 1)

  - base:       来源类型基础置信度 [0.3, 0.9]
  - freshness:  数据时效衰减 [0.5, 1.0]（7 天内 1.0，30 天线性衰减至 0.5）
  - feedback:   用户反馈 [0.7, 1.1]（good×1.1 / bad×0.7 / 未反馈×1.0）
  - corroboration: 多源印证 [1.0, 1.2]（7 天内同结论独立提及 N 次 → 1.0+0.05×min(N-1,4)）

用法:
    from memory.confidence import ConfidenceCalculator

    calc = ConfidenceCalculator()
    result = calc.compute(entry, extracts=[...], dash_meta={...})
    # result = {"confidence": 0.72, "factors": {"base": 0.7, "freshness": 0.95, ...}}
"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

_log = logging.getLogger(__name__)


class ConfidenceCalculator:
    """置信度评分计算器。

    在 archive() 时调用，将计算结果写入 MemoryEntry.confidence / .confidence_factors，
    后续检索时 backend 按 confidence 加权排序、retrieve() 按 confidence_threshold 过滤。
    """

    # 来源 → 基础置信度
    SOURCE_BASE: Dict[str, float] = {
        "tool_hard_data": 0.9,      # 工具硬数据（PE/PB/行情/财报字段）
        "dashboard_llm": 0.7,        # 仪表盘 LLM 输出
        "report_fallback": 0.5,      # 报告文本兜底（正则提取）
        "llm_speculation": 0.3,      # 纯 LLM 推测（无工具调用）
    }

    # freshness 衰减参数
    FRESHNESS_MAX_DAYS = 7    # 7 天内满分
    FRESHNESS_DECAY_DAYS = 30  # 30 天衰减到最低
    FRESHNESS_MIN = 0.5        # 最低新鲜度

    # corroboration 参数
    CORROBORATION_WINDOW_DAYS = 7   # 同结论查最近 7 天
    CORROBORATION_INCREMENT = 0.05  # 每多一次印证加 5%
    CORROBORATION_MAX_EXTRA = 4     # 最多计入 4 次额外印证（上限 N-1=4 → max +0.20）

    # ── 来源推断 ────────────────────────────────────────────

    @classmethod
    def infer_source(cls, extracts: Optional[List[dict]] = None,
                     dash_meta: Optional[dict] = None) -> str:
        """根据可用数据推断记忆来源类型。

        优先级：工具硬数据 > 仪表盘 LLM > 报告兜底 > LLM 推测
        """
        # 有工具提取的硬数据（PE/PB/涨跌幅 等数值字段）
        if extracts:
            for ex in extracts:
                if not isinstance(ex, dict):
                    continue
                hard_fields = {"pe", "pb", "pe_dynamic", "change_pct", "turnover_rate",
                               "total_mv", "float_mv", "price", "roe"}
                if hard_fields & set(k for k, v in ex.items() if v is not None):
                    return "tool_hard_data"
        # 有仪表盘 LLM 输出
        if dash_meta:
            return "dashboard_llm"
        # extracts 有内容但无硬数据 → 报告兜底
        if extracts:
            return "report_fallback"
        # 完全无结构化数据 → LLM 推测
        return "llm_speculation"

    # ── 因子计算 ────────────────────────────────────────────

    def compute_base(self, source: str) -> float:
        """来源类型 → 基础置信度"""
        return self.SOURCE_BASE.get(source, 0.5)

    def compute_freshness(self, date_str: str) -> float:
        """距今天数 → 新鲜度衰减。

        7 天内 → 1.0，7～30 天线性衰减到 0.5，>30 天 → 0.5。
        无法解析日期 → 默认 0.7（中等偏保守）。
        """
        try:
            # 尝试解析 ISO 日期
            if "T" in date_str:
                date_str = date_str[:10]
            entry_date = datetime.strptime(date_str, "%Y-%m-%d")
            days = (datetime.now() - entry_date).days
        except (ValueError, TypeError):
            return 0.7

        if days <= self.FRESHNESS_MAX_DAYS:
            return 1.0
        if days >= self.FRESHNESS_DECAY_DAYS:
            return self.FRESHNESS_MIN
        # 线性衰减
        ratio = (days - self.FRESHNESS_MAX_DAYS) / (self.FRESHNESS_DECAY_DAYS - self.FRESHNESS_MAX_DAYS)
        return round(1.0 - ratio * (1.0 - self.FRESHNESS_MIN), 3)

    def compute_feedback(self, feedback_history: Optional[dict] = None) -> float:
        """用户反馈 → 反馈系数。

        good → 1.1, bad → 0.7, 未反馈 → 1.0。
        feedback_history: {"action": "good"|"bad"|"neutral"}
        """
        if not feedback_history or not isinstance(feedback_history, dict):
            return 1.0
        action = feedback_history.get("action", "")
        if action == "good":
            return 1.1
        if action == "bad":
            return 0.7
        return 1.0

    def compute_corroboration(self, stock_code: str, sentiment: str,
                              recent_conclusions: Optional[List[dict]] = None) -> float:
        """多源印证 → 印证系数。

        查询 7 天内同 stock_code + 同 sentiment 的独立结论数 N：
        N=1 → 1.0, N=2 → 1.05, N=3 → 1.10, ..., N≥5 → 1.20
        """
        if not stock_code or not sentiment or not recent_conclusions:
            return 1.0

        same = 0
        for c in recent_conclusions:
            if not isinstance(c, dict):
                continue
            if c.get("stock_code") == stock_code and c.get("sentiment") == sentiment:
                same += 1

        if same <= 1:
            return 1.0
        extra = min(same - 1, self.CORROBORATION_MAX_EXTRA)
        return round(1.0 + self.CORROBORATION_INCREMENT * extra, 3)

    # ── 合成 ────────────────────────────────────────────────

    def compute(self,
                entry=None,                    # MemoryEntry（用于读取现有字段）
                extracts: Optional[List[dict]] = None,
                dash_meta: Optional[dict] = None,
                feedback_history: Optional[dict] = None,
                recent_conclusions: Optional[List[dict]] = None,
                source: Optional[str] = None,
                ) -> dict:
        """计算置信度，返回 {"confidence": float, "factors": dict}。

        Args:
            entry: 记忆条目（读取 date/stock_code/metadata.sentiment）
            extracts: 工具提取列表（用于来源推断）
            dash_meta: 仪表盘元数据
            feedback_history: 用户反馈记录
            recent_conclusions: 最近同 stock 结论列表（用于印证计算）
            source: 显式指定来源类型（覆盖自动推断）
        """
        if entry is None:
            # 无条目上下文时返回默认中等置信度
            return {"confidence": 0.5, "factors": {"base": 0.5, "freshness": 1.0,
                                                     "feedback": 1.0, "corroboration": 1.0}}

        # 1. 来源 → base
        if source is None:
            source = self.infer_source(extracts, dash_meta)
        base = self.compute_base(source)

        # 2. 日期 → freshness
        date_str = entry.metadata.get("date", "") if entry.metadata else ""
        freshness = self.compute_freshness(date_str)

        # 3. 反馈 → feedback
        feedback = self.compute_feedback(feedback_history)

        # 4. 印证 → corroboration
        stock_code = getattr(entry, "stock_code", "") or ""
        sentiment = entry.metadata.get("sentiment", "") if entry.metadata else ""
        corroboration = self.compute_corroboration(stock_code, sentiment, recent_conclusions)

        # 合成
        confidence = base * freshness * feedback * corroboration
        confidence = max(0.0, min(1.0, confidence))

        return {
            "confidence": round(confidence, 4),
            "factors": {
                "source": source,
                "base": base,
                "freshness": round(freshness, 3),
                "feedback": round(feedback, 3),
                "corroboration": round(corroboration, 3),
            },
        }


# 模块级单例
_calculator: Optional[ConfidenceCalculator] = None


def get_confidence_calculator() -> ConfidenceCalculator:
    global _calculator
    if _calculator is None:
        _calculator = ConfidenceCalculator()
    return _calculator

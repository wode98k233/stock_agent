"""
评测器基类
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class EvalResult:
    """单个维度的评测结果"""
    dimension: str       # 维度名
    score: float         # 0-100
    weight: float        # 权重
    is_auto: bool        # 是否自动评测
    detail: Dict         # 明细数据
    reason: str = ""     # 评测理由


class BaseEvaluator(ABC):
    """评测器基类"""

    dimension: str = ""
    weight: float = 0.2
    is_auto: bool = True

    @abstractmethod
    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        """执行评测，返回结果"""
        ...

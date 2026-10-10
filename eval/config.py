"""
评测系统配置
"""
from dataclasses import dataclass, field
from typing import Dict, List

from config import Config


@dataclass
class EvalConfig:
    """评测系统配置"""

    # 评测维度权重
    weights: Dict[str, float] = field(default_factory=lambda: {
        "tool_correctness": 0.20,
        "output_completeness": 0.15,
        "factual_accuracy": 0.30,
        "reasoning_quality": 0.25,
        "efficiency": 0.10,
    })

    # 启用的评测器
    enabled_evaluators: List[str] = field(default_factory=lambda: [
        "tool", "output", "efficiency", "factual", "llm_judge",
    ])

    # LLM-Judge 模型（从 Config 读取，优先用 JUDGE_LLM_MODEL 配置的第三方模型）
    llm_judge_model: str = ""

    # LLM-Judge 超时（秒）
    llm_judge_timeout: int = 60

    # Agent 响应截断长度（防止 token 爆炸）
    max_response_chars: int = 4000

    # 并发执行数（默认串行）
    concurrency: int = 1

    # 效率阈值（按难度）
    efficiency_thresholds: Dict[str, dict] = field(default_factory=lambda: {
        "easy":   {"steps": 3,  "tokens": 2000,  "time": 8},
        "medium": {"steps": 6,  "tokens": 4000,  "time": 15},
        "hard":   {"steps": 10, "tokens": 8000,  "time": 30},
    })

    # Benchmark 数据集路径
    benchmark_path: str = "eval/benchmark_dataset.json"

    # 评测结果保存目录
    results_dir: str = "eval/results"


# 全局配置实例
_config: EvalConfig | None = None


def get_eval_config() -> EvalConfig:
    """获取全局评测配置（单例）"""
    global _config
    if _config is None:
        _config = EvalConfig()
        # 从 Config 读取 Judge 模型配置
        _config.llm_judge_model = Config.JUDGE_LLM_MODEL or Config.OPENAI_MODEL_NAME
    return _config


def set_eval_config(config: EvalConfig):
    """设置全局评测配置"""
    global _config
    _config = config

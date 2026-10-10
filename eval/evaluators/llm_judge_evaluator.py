"""
LLM-Judge 评测器（AI 裁判）

用独立的 Judge 模型对 Agent 回答进行事实准确性和推理质量评分。
通过 llm_factory 复用项目的 LLM 调用基础设施（token 跟踪、trace、JSON 重试）。

关键设计：
- Judge 模型应与被评测 Agent 使用的模型不同，避免"同模型自评"导致评分失真。
- 通过 .env 的 JUDGE_LLM_MODEL / JUDGE_LLM_API_KEY / JUDGE_LLM_API_BASE 配置第三方模型。
- 如果未配置 Judge 模型，fallback 到默认 LLM，但会发出 warning。
"""
import json
from typing import Dict, List

from config import Config
from utils.llm_factory import get_judge_llm, llm_json_with_retry, extract_json
from utils.logger import get_logger

from .base_evaluator import BaseEvaluator, EvalResult

_logger = get_logger("eval_judge", skip_db=True)


class LLMJudgeEvaluator(BaseEvaluator):
    """
    LLM-Judge 评测器
    一次 LLM 调用，同时评分 factual_accuracy 和 reasoning_quality 两个维度。

    优先使用 JUDGE_LLM_* 配置的第三方模型；
    如果未配置，fallback 到默认 LLM 并发出 warning。
    """

    dimension = "llm_judge"  # 特殊：返回两个维度
    weight = 0.0  # 不参与加权（由 scoring 模块处理）
    is_auto = False

    def __init__(self, model: str = "", timeout: int = 60):
        """
        Args:
            model: 可选，手动指定 Judge 模型名（覆盖 .env 配置）
            timeout: LLM 调用超时秒数
        """
        self._llm = None
        self._timeout = timeout
        self._model_name = model  # 可选覆盖
        self._available = bool(Config.OPENAI_API_KEY)

        # 检查 Judge 模型与 Agent 模型是否相同
        self._check_model_conflict()

    def _check_model_conflict(self):
        """如果 Judge 模型和 Agent 模型相同，发出 warning"""
        judge_model = Config.JUDGE_LLM_MODEL or self._model_name or Config.OPENAI_MODEL_NAME
        agent_model = Config.OPENAI_MODEL_NAME
        if judge_model and agent_model and judge_model == agent_model:
            _logger.warning(
                "W",
                f"Judge 模型({judge_model}) 与 Agent 模型({agent_model}) 相同，"
                "同模型自评可能导致评分失真。建议在 .env 中配置 JUDGE_LLM_MODEL 使用不同的第三方模型。"
            )

    def _get_llm(self):
        """获取 Judge LLM 实例（惰性初始化）"""
        if self._llm is not None:
            return self._llm
        # 优先用 JUDGE_LLM_* 配置的第三方模型
        self._llm = get_judge_llm()
        return self._llm

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        """
        用 LLM 评分 factual_accuracy 和 reasoning_quality
        返回 EvalResult，但 detail 里包含两个维度的分数
        """
        if not self._available:
            return EvalResult(
                dimension="llm_judge",
                score=-1.0,  # -1 表示跳过
                weight=0.0,
                is_auto=False,
                detail={"skipped": True, "reason": "未配置 OPENAI_API_KEY"},
                reason="未配置 API Key，跳过 LLM-Judge",
            )

        max_chars = 4000
        if len(agent_response) > max_chars:
            agent_response = agent_response[:max_chars] + "\n[截断...]"

        prompt = self._build_prompt(benchmark_case, agent_response)

        try:
            scores = self._call_llm(prompt)
            return EvalResult(
                dimension="llm_judge",
                score=scores.get("factual_accuracy", 0.0),
                weight=0.0,
                is_auto=False,
                detail={
                    "factual_accuracy": scores.get("factual_accuracy", 0.0),
                    "reasoning_quality": scores.get("reasoning_quality", 0.0),
                    "reason": scores.get("reason", ""),
                },
                reason=scores.get("reason", ""),
            )
        except Exception as e:
            _logger.error("E", f"LLM-Judge 调用失败: {e}")
            return EvalResult(
                dimension="llm_judge",
                score=-1.0,
                weight=0.0,
                is_auto=False,
                detail={"error": str(e)},
                reason=f"LLM-Judge 调用失败: {e}",
            )

    def evaluate_two_dimensions(
        self,
        benchmark_case: Dict,
        agent_response: str,
    ) -> Dict:
        """
        返回两个维度的分数（供 run_eval.py 直接调用）
        返回: {"factual_accuracy": 80.0, "reasoning_quality": 75.0, "reason": "..."}
        """
        if not self._available:
            return {
                "factual_accuracy": -1.0,
                "reasoning_quality": -1.0,
                "reason": "未配置 OPENAI_API_KEY",
            }

        max_chars = 4000
        if len(agent_response) > max_chars:
            agent_response = agent_response[:max_chars] + "\n[截断...]"

        prompt = self._build_prompt(benchmark_case, agent_response)

        try:
            return self._call_llm(prompt)
        except Exception as e:
            _logger.error("E", f"LLM-Judge 调用失败: {e}")
            return {
                "factual_accuracy": -1.0,
                "reasoning_quality": -1.0,
                "reason": f"LLM-Judge 调用失败: {e}",
            }

    def _build_prompt(self, case: Dict, response: str) -> str:
        """构造 LLM-Judge 评分 Prompt"""
        question = case.get("question", "")
        expected = case.get("expected_output", "") or case.get("reference_answer", "") or ""
        expected_tools = case.get("expected_tools", [])
        if isinstance(expected_tools, str):
            try:
                expected_tools = json.loads(expected_tools)
            except Exception:
                expected_tools = []

        return f"""你是 Stock Radar 评测系统的 AI 裁判。请根据以下信息，对 Agent 的回答进行评分。

## 用户问题
{question}

## Agent 回答
{response}

## 参考答案（可选）
{expected}

## 期望调用的工具
{", ".join(expected_tools) if expected_tools else "无"}

---

请对以下两个维度分别打分（0-100 分），并以 JSON 格式返回：

1. **factual_accuracy（事实准确性，30%权重）**
   - 回答中的股票代码、价格、指标数据是否准确
   - 是否出现幻觉（编造不存在的数据）
   - 评分标准：90+ = 无错误；70-89 = 少量不精确；50-69 = 有明显错误；<50 = 严重幻觉

2. **reasoning_quality（推理质量，25%权重）**
   - 分析逻辑是否连贯、有深度
   - 是否给出了可操作的结论
   - 评分标准：90+ = 分析深入、结论明确；70-89 = 有分析但深度不足；50-69 = 泛泛而谈；<50 = 逻辑混乱

返回格式（严格遵守 JSON 格式，不要有多余符号）：
```json
{{
  "factual_accuracy": 80.0,
  "reasoning_quality": 75.0,
  "reason": "事实准确性较好，但推理部分可以更深入"
}}
```"""

    def _call_llm(self, prompt: str) -> Dict:
        """
        通过 llm_factory 调用 Judge LLM，自动获得：
        - token 跟踪（metrics + budget）
        - trace 集成（通过全局 hook 自动注入）
        - JSON 提取 + 重试
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = self._get_llm()
        messages = [
            SystemMessage(content="你是一个严格的 AI 评测裁判，只返回 JSON 格式的结果。"),
            HumanMessage(content=prompt),
        ]

        result = llm_json_with_retry(
            llm,
            messages,
            _logger,
            label="llm_judge",
            max_retries=2,
        )

        if not isinstance(result, dict):
            result = extract_json(result) or {}

        return {
            "factual_accuracy": float(result.get("factual_accuracy", 0.0)),
            "reasoning_quality": float(result.get("reasoning_quality", 0.0)),
            "reason": str(result.get("reason", "")),
        }

"""
评测 CLI 入口
用法:
  python eval/run_eval.py                          # 运行全部启用 case
  python eval/run_eval.py --mode plan_solve        # 指定 Agent 模式
  python eval/run_eval.py --categories stock_analysis,valuation  # 指定分类
  python eval/run_eval.py --difficulty easy         # 指定难度
  python eval/run_eval.py --tag v1.3 --name "v1.3 回归"  # 版本标签
  python eval/run_eval.py --no-llm-judge           # 跳过 LLM-Judge
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# 确保项目根目录在 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from eval.config import get_eval_config, EvalConfig
from eval.storage.eval_db import get_eval_storage
from eval.runners.agent_runner import AgentRunner
from eval.evaluators.base_evaluator import EvalResult
from eval.evaluators.tool_evaluator import ToolExecutionEvaluator, ToolSelectionEvaluator
from eval.evaluators.output_evaluator import OutputEvaluator
from eval.evaluators.data_freshness_evaluator import DataFreshnessEvaluator
from eval.evaluators.context_efficiency_evaluator import ContextEfficiencyEvaluator
from eval.evaluators.budget_evaluator import BudgetEvaluator
from eval.evaluators.constraint_evaluator import ConstraintComplianceEvaluator
from eval.evaluators.llm_judge_evaluator import LLMJudgeEvaluator
from eval.metrics.scoring import ScoringAggregator
from utils.logger import get_logger, get_child_logger

# 评测 CLI 专用 logger（不写 DB）
_logger, _, _, _ = get_logger("eval_cli", skip_db=True)


def load_benchmark_cases(config: EvalConfig) -> List[Dict]:
    """加载 benchmark 数据集"""
    path = ROOT / config.benchmark_path
    if not path.exists():
        _logger.warning("W", f"benchmark 文件不存在: {path}，将只运行启用的 case")
        # 从 DB 加载
        storage = get_eval_storage()
        return storage.list_benchmark()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", [])


def filter_cases(
    cases: List[Dict],
    categories: List[str] | None = None,
    difficulty: str | None = None,
    case_ids: List[str] | None = None,
) -> List[Dict]:
    """按条件过滤 case"""
    result = []
    for c in cases:
        if not c.get("enabled", True):
            continue
        if case_ids and c["id"] not in case_ids:
            continue
        if categories and c.get("category") not in categories:
            continue
        if difficulty and c.get("difficulty") != difficulty:
            continue
        result.append(c)
    return result


def run_eval(config: EvalConfig, args) -> Dict:
    """
    执行评测，返回结果汇总
    """
    # 加载 benchmark
    all_cases = load_benchmark_cases(config)
    cases = filter_cases(
        all_cases,
        categories=args.categories,
        difficulty=args.difficulty,
    )
    if not cases:
        _logger.error("E", "没有匹配的 benchmark case")
        return {}

    _logger.info("=", f"Stock Radar Agent 评测 | 版本: {args.tag or 'dev'} | 模式: {args.mode} | Case 数: {len(cases)}")

    # 创建评测批次
    storage = get_eval_storage()
    run_id = f"eval_{uuid.uuid4().hex[:8]}"
    storage.create_run(
        run_id=run_id,
        name=args.name or f"评测 {args.tag or 'dev'}",
        mode=args.mode,
        tag=args.tag or "",
        config={"categories": args.categories, "difficulty": args.difficulty},
        llm_judge_model=config.llm_judge_model,
    )

    # 初始化运行器
    runner = AgentRunner(agent_mode=args.mode)

    # 初始化评测器（回归层 7 条，全规则；维度与权重见 eval/dimensions.py）
    evaluators = [
        ToolSelectionEvaluator(),
        ToolExecutionEvaluator(),
        OutputEvaluator(),
        DataFreshnessEvaluator(),
        ContextEfficiencyEvaluator(),
        BudgetEvaluator(),
        ConstraintComplianceEvaluator(),
    ]
    # LLM-Judge（旧口径 factual/reasoning，回归层默认关闭，--llm-judge 显式开启）
    llm_judge = None
    if args.llm_judge:
        llm_judge = LLMJudgeEvaluator(
            model=config.llm_judge_model,
            timeout=config.llm_judge_timeout,
        )
    aggregator = ScoringAggregator()

    # 批量运行 + 评测
    results = []
    for i, case in enumerate(cases):
        _logger.info("R", f"[{i+1}/{len(cases)}] {case['id']}: {case['question'][:30]}...")

        # 创建 eval_case 记录
        case_db_id = storage.create_eval_case(run_id, case)

        # 运行 Agent
        run_data = runner.run_single(case)

        # 保存运行结果
        storage.update_eval_case(case_db_id, **{
            "trace_run_id": run_data.get("trace_run_id"),
            "agent_response": run_data.get("agent_response", ""),
            "tool_calls_json": run_data.get("tool_calls", []),
            "iteration_count": run_data.get("iteration_count", 0),
            "tool_calls_count": run_data.get("tool_calls_count", 0),
            "tokens_in": run_data.get("tokens_in", 0),
            "tokens_out": run_data.get("tokens_out", 0),
            "elapsed_seconds": run_data.get("elapsed_seconds", 0),
            "limit_reason": run_data.get("limit_reason"),
            "status": run_data.get("status", "error"),
        })

        if run_data.get("status") == "error":
            _logger.warning("F", f"运行失败: {run_data.get('error', 'unknown')}")
            failed = (storage.get_run(run_id) or {}).get("failed_cases") or 0
            storage.update_run(run_id, failed_cases=failed + 1)
            continue

        # 评测
        eval_results = []
        for evaluator in evaluators:
            try:
                result = evaluator.evaluate(
                    benchmark_case=case,
                    agent_response=run_data.get("agent_response", ""),
                    tool_calls=run_data.get("tool_calls", []),
                    run_metrics={
                        "iteration_count": run_data.get("iteration_count", 0),
                        "tokens_in": run_data.get("tokens_in", 0),
                        "tokens_out": run_data.get("tokens_out", 0),
                        "elapsed_seconds": run_data.get("elapsed_seconds", 0),
                    },
                    trace_run_id=run_data.get("trace_run_id"),
                )
                eval_results.append(result)

                # 保存打分
                storage.save_score(
                    case_id=case_db_id,
                    dimension=result.dimension,
                    score=result.score,
                    weight=result.weight,
                    evaluator=type(evaluator).__name__,
                    is_auto=result.is_auto,
                    detail=result.detail,
                    reason=result.reason,
                )
            except Exception as e:
                _logger.warning("W", f"评测器 {type(evaluator).__name__} 失败: {e}")

        # LLM-Judge 评分（事实准确性 + 推理质量）
        if llm_judge:
            try:
                llm_scores = llm_judge.evaluate_two_dimensions(
                    benchmark_case=case,
                    agent_response=run_data.get("agent_response", ""),
                )
                # 保存事实准确性分数
                if llm_scores.get("factual_accuracy", -1.0) >= 0:
                    result_f = EvalResult(
                        dimension="factual_accuracy",
                        score=llm_scores["factual_accuracy"],
                        weight=config.weights.get("factual_accuracy", 0.30),
                        is_auto=False,
                        detail={},
                        reason=llm_scores.get("reason", ""),
                    )
                    eval_results.append(result_f)
                    storage.save_score(
                        case_id=case_db_id,
                        dimension="factual_accuracy",
                        score=llm_scores["factual_accuracy"],
                        weight=config.weights.get("factual_accuracy", 0.30),
                        evaluator="LLMJudgeEvaluator",
                        is_auto=False,
                        detail={},
                        reason=llm_scores.get("reason", ""),
                    )
                # 保存推理质量分数
                if llm_scores.get("reasoning_quality", -1.0) >= 0:
                    result_r = EvalResult(
                        dimension="reasoning_quality",
                        score=llm_scores["reasoning_quality"],
                        weight=config.weights.get("reasoning_quality", 0.25),
                        is_auto=False,
                        detail={},
                        reason=llm_scores.get("reason", ""),
                    )
                    eval_results.append(result_r)
                    storage.save_score(
                        case_id=case_db_id,
                        dimension="reasoning_quality",
                        score=llm_scores["reasoning_quality"],
                        weight=config.weights.get("reasoning_quality", 0.25),
                        evaluator="LLMJudgeEvaluator",
                        is_auto=False,
                        detail={},
                        reason=llm_scores.get("reason", ""),
                    )
                _logger.info("J", f"[Judge] 事实:{llm_scores.get('factual_accuracy', '-')} 推理:{llm_scores.get('reasoning_quality', '-')}")
            except Exception as e:
                _logger.warning("W", f"LLM-Judge 失败: {e}")

        # 聚合打分
        agg = aggregator.aggregate(eval_results)

        # 保存聚合结果
        storage.update_eval_case(case_db_id, **{
            "overall_score": agg["overall"],
            "scores_json": agg["dimensions"],
            "status": "scored",
            "scored_at": datetime.now(timezone.utc).isoformat(),
        })

        results.append({
            "case_id": case["id"],
            "overall": agg["overall"],
            "dimensions": agg["dimensions"],
        })

        _logger.info("S", f"得分: {agg['overall']}  ["
              f"选择:{agg['dimensions'].get('tool_selection', {}).get('score', '-')} "
              f"执行:{agg['dimensions'].get('tool_execution', {}).get('score', '-')} "
              f"输出:{agg['dimensions'].get('output_completeness', {}).get('score', '-')}]")

        # 更新批次进度
        storage.update_run(run_id, completed_cases=i+1)

    # 更新状态为 completed
    storage.update_run(run_id, status="completed")

    # 汇总（更新状态后再取，保证报告显示 completed）
    summary = storage.get_run_summary(run_id)
    print_summary(summary)

    # 保存快照
    storage.save_snapshot(
        run_id=run_id,
        version_tag=args.tag or "",
        overall_score=summary.get("overall_score"),
        scores_json=summary.get("dimensions", {}),
        category_scores=summary.get("category_scores", {}),
        efficiency_json={},
        case_count=len(cases),
    )

    return summary


def print_summary(summary: Dict):
    """打印评测汇总报告"""
    overall = summary.get('overall_score') or 0
    _logger.info("=", f"评测报告 | 总分: {overall}/100 | 状态: {summary.get('status', 'unknown')} | "
              f"通过: {summary.get('completed_cases', 0)} 失败: {summary.get('failed_cases', 0)}")

    dims = summary.get("dimensions", {})
    for dim, val in dims.items():
        if isinstance(val, dict):
            score = val.get("score", 0) or 0
        else:
            score = val or 0
        _logger.info("D", f"  {dim:20s}: {score:5.1f}")


def main():
    parser = argparse.ArgumentParser(description="Stock Radar Agent 评测工具")
    parser.add_argument("--mode", default="react_stock", help="Agent 模式")
    parser.add_argument("--categories", help="分类过滤（逗号分隔）")
    parser.add_argument("--difficulty", help="难度过滤（easy/medium/hard）")
    parser.add_argument("--tag", help="版本标签")
    parser.add_argument("--name", help="批次名称")
    parser.add_argument("--llm-judge", action="store_true",
                        help="附加旧口径 LLM-Judge（factual/reasoning）。回归层默认全规则，不需要它")
    parser.add_argument("--no-llm-judge", action="store_true",
                        help="（已废弃）回归层默认就不跑 LLM-Judge，仅为兼容保留")
    args = parser.parse_args()

    if args.categories:
        args.categories = [c.strip() for c in args.categories.split(",")]
    else:
        args.categories = None

    config = get_eval_config()
    if args.no_llm_judge:
        config.enabled_evaluators = [e for e in config.enabled_evaluators if e != "llm_judge"]

    run_eval(config, args)


if __name__ == "__main__":
    main()

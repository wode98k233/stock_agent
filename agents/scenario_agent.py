"""
选股雷达 - 场景Agent（快速路径）

根据意图分类结果，路由到对应的场景处理器。
继承 BaseAgent，注册到 AgentFactory，用户通过 mode scenario 切换。
"""
import logging
import sys
import traceback
import asyncio
from typing import Optional, Callable

from agents.base import BaseAgent
from agents.scenario_router import Scenario, classify_scenario
from agents.run_context import AgentRunContext
from agents.scenarios.common import ScenarioResult
from output.time_util import get_data_timestamp
from utils.progress import ProgressReporter, ProgressType
from utils.logger import ensure_radar
from utils.budget import BudgetControllerFactory, BudgetExceeded
from config import Config

logger = logging.getLogger("radar.scenario")


class ScenarioAgent(BaseAgent):
    name = "scenario"
    display_name = "Scenario"
    description = "正则快速路径，低延迟处理常见场景。"

    def __init__(self):
        self._handlers = {}
        self._register_handlers()

    def _register_handlers(self):
        from agents.scenarios.screening import handle_screening
        from agents.scenarios.stock_analysis import handle_stock_analysis
        from agents.scenarios.sector_analysis import handle_sector_analysis
        from agents.scenarios.market_overview import handle_market_overview
        from agents.scenarios.data_query import handle_data_query
        from agents.scenarios.comparison import handle_comparison

        self._handlers = {
            Scenario.SCREENING: handle_screening,
            Scenario.STOCK_ANALYSIS: handle_stock_analysis,
            Scenario.SECTOR_ANALYSIS: handle_sector_analysis,
            Scenario.MARKET_OVERVIEW: handle_market_overview,
            Scenario.DATA_QUERY: handle_data_query,
            Scenario.COMPARISON: handle_comparison,
        }

    async def run(self, user_input: str, registry, memory, logger,
                  progress_callback: Optional[Callable] = None, **kwargs) -> str:
        logger = ensure_radar(logger)
        reporter = ProgressReporter(progress_callback)
        final_result = ""

        # 1. 意图分类
        reporter.report(ProgressType.CLASSIFIER, "正在识别意图...")
        scenario, context = classify_scenario(user_input)

        if scenario is None:
            logger.info("场景Agent: 未匹配任何场景")
            return ""

        logger.info(f"场景Agent: 匹配到 {scenario.value}")
        reporter.report(ProgressType.MILESTONE, f"命中场景: {scenario.value}")

        handler = self._handlers.get(scenario)
        if not handler:
            logger.warning(f"场景Agent: 无处理器 {scenario.value}")
            return ""

        # 初始化预算控制器
        budget = BudgetControllerFactory.create()

        # 使用 AgentRunContext 包裹执行
        with AgentRunContext("scenario", logger, user_input) as run_ctx:
            try:
                # 检查预算（启动时）
                budget.check(logger)

                reporter.report(ProgressType.STEP_START, f"正在执行 {scenario.value}...")
                enriched_input = self._enrich_user_input(
                    user_input, progress_callback=progress_callback,
                    history=memory.get_history() if memory else None,
                )
                data_timestamp = get_data_timestamp()

                result = await handler(
                    user_input=user_input,
                    enriched_input=enriched_input,
                    context=context,
                    data_timestamp=data_timestamp,
                    budget=budget,  # 传递预算给 handler
                )

                if result:
                    budget.check(logger)

                    reporter.report(ProgressType.FINAL, "场景分析完成")
                    final_result = result.text
                    scenario_data = result.data

                    if memory:
                        try:
                            memory.append_turn(user_input, final_result[:500])
                        except Exception as e:
                            logger.warning(f"场景记忆写入失败: {e}")

                    if Config.REPORT_ENABLE_ANALYSIS_ENGINE and scenario_data:
                        from agents.analysis.scenario_adapter import get_template_id_for_scenario, build_evidence_from_scenario
                        template_id = get_template_id_for_scenario(scenario)
                        if template_id:
                            try:
                                from agents.analysis import run_analysis
                                from agents.analysis.models import AnalysisRequest
                                evidence = build_evidence_from_scenario(scenario, scenario_data)
                                fake_tool_calls = evidence.get("items", [])
                                analysis = await run_analysis(
                                    request=AnalysisRequest(
                                        user_input=user_input,
                                        agent_name="scenario",
                                        logger=logger,
                                        budget=budget,
                                        template_id=template_id,
                                    ),
                                    tool_calls=fake_tool_calls,
                                    raw_result=final_result,
                                )
                                if analysis.content and not analysis.fallback_used:
                                    final_result = analysis.content
                            except Exception as e:
                                logger.warning(f"场景模板后处理失败，使用原始结果: {e}")

                    # 仪表盘生成（独立于分析引擎）
                    if Config.DASHBOARD_ENABLED and scenario_data:
                        try:
                            from agents.analysis.dashboard_node import append_dashboard
                            from agents.analysis.scenario_adapter import get_template_id_for_scenario

                            template_id = get_template_id_for_scenario(scenario)
                            final_result = await append_dashboard(
                                report=final_result,
                                slot_results=scenario_data,
                                template_id=template_id,
                                user_input=user_input,
                                budget=budget,
                                logger=logger,
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ 仪表盘生成失败: {e}")

                    # 更新意图记忆
                    result_str = final_result if isinstance(final_result, str) else str(final_result or "")
                    self._update_intent_memory(user_input, result_str, logger)

                    # 结束 trace
                    run_ctx.end_trace(result, status="success")
                else:
                    logger.info(f"场景Agent: {scenario.value} 返回空")
                    run_ctx.end_trace("", status="no_result")

            except BudgetExceeded as e:
                error_msg = f"场景Agent: 预算超限: {e}"
                logger.error(error_msg)
                reporter.report(ProgressType.ERROR, f"预算超限，请稍后再试")
                run_ctx.end_trace("", status="error", error=str(e))

            except Exception as e:
                error_msg = f"场景Agent: {scenario.value} 异常: {e}\n{traceback.format_exc()}"
                logger.error(error_msg)
                reporter.report(ProgressType.ERROR, f"场景执行异常: {e}")
                run_ctx.end_trace("", status="error", error=str(e))

        return final_result

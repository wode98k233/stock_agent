"""
选股雷达 - 场景Agent（快速路径）

根据意图分类结果，路由到对应的场景处理器。
继承 BaseAgent，注册到 AgentFactory，用户通过 mode scenario 切换。
"""
import logging
import traceback
import asyncio
from typing import Optional, Callable

from agents.base import BaseAgent
from agents.scenario_router import Scenario, classify_scenario
from agents.run_context import AgentRunContext
from output.time_util import get_data_timestamp
from utils.progress import ProgressReporter, ProgressType
from utils.logger import ensure_radar
from utils.budget import BudgetControllerFactory, BudgetExceeded

logger = logging.getLogger("radar.scenario")


class ScenarioAgent(BaseAgent):
    name = "scenario"
    description = "场景快速路径 Agent（正则分类，低延迟）"

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
                  progress_callback: Optional[Callable] = None) -> str:
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
                enriched_input = self._enrich_user_input(user_input)
                data_timestamp = get_data_timestamp()

                result = await handler(
                    user_input=user_input,
                    enriched_input=enriched_input,
                    context=context,
                    data_timestamp=data_timestamp,
                    budget=budget,  # 传递预算给 handler
                )

                if result:
                    # 检查预算（完成时）
                    budget.check(logger)

                    reporter.report(ProgressType.FINAL, "场景分析完成")
                    final_result = result

                    if memory:
                        try:
                            memory.save_context(
                                {"input": user_input},
                                {"output": result[:500]},
                            )
                        except Exception:
                            pass

                    # 更新意图记忆
                    self._update_intent_memory(user_input, result, logger)

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

    def _update_intent_memory(self, user_input: str, final_response: str, logger):
        """更新意图记忆（类似 plan_solve）"""
        try:
            from utils.intent import IntentMemoryManager
            intent_mgr = IntentMemoryManager(user_id="default")
            sectors, stocks = intent_mgr.extract_entities(final_response or "")
            intent_mgr.update(
                query=user_input,
                result=final_response or "",
                sectors=sectors,
                stocks=stocks,
            )
        except Exception as e:
            logger.debug(f"[IntentMemory] 更新失败（非阻塞）: {e}")

    def on_startup(self):
        """启动时清理过期缓存"""
        from utils.cache import async_clean_expired_cache
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(async_clean_expired_cache())
        except RuntimeError:
            pass

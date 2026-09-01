# -*- coding: utf-8 -*-
"""GroupAgent 主入口 — 继承 PlanBasedAgent"""
from agents.plan.base import PlanBasedAgent
from agents.group.dispatcher import build_dispatch_graph
from agents.group.state import GroupState
from agents.group.exception_handlers import (
    GroupExceptionHandlerChain, GroupException, EmptyPlanGroupException,
)
from agents.agent_context import AgentContext
from agents.common import select_and_load_template
from config import Config
from utils.progress import ProgressReporter
from utils.budget import BudgetControllerFactory, BudgetLimits


class GroupAgent(PlanBasedAgent):
    name = "agent_group"
    display_name = "Agent Group"
    description = "多个智能体合作，适合复杂任务。"

    def __init__(self):
        super().__init__()
        self._exception_handlers = GroupExceptionHandlerChain()

    def _create_budget(self):
        """Agent Group 使用更高的 LLM 调用预算（多 agent 共享）"""
        return BudgetControllerFactory.create(limits=BudgetLimits(
            max_tokens_per_query=Config.MAX_TOKENS_PER_QUERY,
            max_llm_calls_per_query=Config.GROUP_MAX_LLM_CALLS,
            max_time_seconds=Config.MAX_TIME_SECONDS,
        ))

    def _build_graph(self, logger, memory, registry, progress_callback, budget, checkpointer):
        progress = ProgressReporter(progress_callback) if progress_callback else None
        ctx = AgentContext(
            logger=logger,
            memory=memory,
            skill_registry=registry,
            budget=budget,
            progress_reporter=progress,
        )
        return build_dispatch_graph(ctx, checkpointer)

    def _build_initial_state(self, user_input, template_id, selected_skills, **kwargs):
        return GroupState(
            input=user_input,
            plan_steps=[],
            current_step_index=0,
            original_plan=[],
            accumulated_data="",
            resolved_data="",
            constraints="",
            observation="",
            response="",
            data_collection_doc="",
            budget_exempt=None,
            _replan_count=0,
            _failed_agents=None,
            _error_message=None,
            template_id=template_id,
            tool_calls=[],
            selected_skills=selected_skills or [],
            dialog_uuid=kwargs.get("dialog_uuid", ""),
            task_id=kwargs.get("task_id", ""),
            current_agent_names=[],
            current_task_purpose="",
            dispatch_history=[],
        )

    def _build_callbacks_tag(self) -> str:
        return "agent_group"

    def _get_recursion_limit(self) -> int:
        return Config.GROUP_MAX_STEPS * 8 + 20

    def _extract_completed_steps(self, last_state) -> list:
        steps = last_state.get("plan_steps", [])
        return [
            (s["task_purpose"], s.get("result", ""))
            for s in steps if s.get("status") == "success"
        ]

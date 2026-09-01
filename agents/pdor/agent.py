"""PDOR Agent 入口"""

from agents.plan.base import PlanBasedAgent
from agents.pdor import build_pdor_graph
from config import Config


class PdorAgent(PlanBasedAgent):
    """PDOR 模式 Agent"""

    name = "pdor"
    display_name = "PDOR"
    description = "Plan-Do-Observe-Reflect 循环，适合需要观察和反思的复杂任务。"

    def _build_graph(self, logger, memory, registry, progress_callback, budget, checkpointer):
        return build_pdor_graph(
            logger=logger,
            memory_mgr=memory,
            skill_registry=registry,
            progress_callback=progress_callback,
            budget=budget,
            checkpointer=checkpointer,
        )

    def _build_initial_state(self, user_input, template_id, selected_skills) -> dict:
        return {
            "input": user_input,
            "plan_steps": [],
            "current_step_index": 0,
            "original_plan": [],
            "info_accumulator": "",
            "observation": "",
            "constraints": "",
            "response": "",
            "budget_exempt": None,
            "_replan_count": 0,
            "_failed_tools": [],
            "template_id": template_id,
            "selected_skills": selected_skills,
            "tool_calls": [],
        }

    def _build_callbacks_tag(self) -> str:
        return "pdor"

    def _get_recursion_limit(self) -> int:
        return Config.PLAN_MAX_STEPS * 8 + 20

    def _extract_completed_steps(self, last_state) -> list[tuple[str, str]]:
        plan_steps = last_state.get("plan_steps", [])
        return [
            (f"Step {s['step']}: {s['purpose']}", s.get("result", ""))
            for s in plan_steps
            if s.get("status") == "success"
        ]

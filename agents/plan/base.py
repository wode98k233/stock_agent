"""Plan/PDOR/Unified 共享基类 — 模板方法模式

提取三个 agent 的公共流程：
- run() 模板方法
- 异常处理（策略模式，复用 ExceptionHandlerChain）
- 用户交互回调

子类只需实现钩子方法：
- _build_graph(): 创建 LangGraph 编译图
- _build_initial_state(): 构建初始状态
- _build_callbacks_tag(): 回调标签（"plan"/"unified"/"pdor"）
- _get_recursion_limit(): 递归限制
- _extract_completed_steps(): 从 last_state 提取已完成步骤
"""
from abc import abstractmethod

from agents.base import BaseAgent
from agents.run_context import AgentRunContext
from agents.common import select_and_load_template
from agents.checkpoint_factory import create_checkpointer
from agents.plan.exception_handlers import PlanHandlerContext, build_plan_handler_chain
from agents.shared.stream_utils import stream_and_collect
from config import Config
from utils.logger import ensure_radar
from utils.budget import BudgetControllerFactory


class PlanBasedAgent(BaseAgent):
    """Plan/PDOR/Unified 共享基类 — 模板方法模式"""

    def __init__(self):
        super().__init__()
        self._exception_handlers = build_plan_handler_chain()

    # ── 子类必须实现的钩子 ──

    @abstractmethod
    def _build_graph(self, logger, memory, registry, progress_callback, budget, checkpointer):
        """创建 LangGraph 编译图"""
        ...

    @abstractmethod
    def _build_callbacks_tag(self) -> str:
        """回调标签（"plan"/"unified"/"pdor"）"""
        ...

    # ── 子类可选覆盖的钩子（提供合理默认值） ──

    def _build_initial_state(self, user_input, template_id, selected_skills, **kwargs) -> dict:
        """构建初始状态 dict"""
        return {
            "input": user_input,
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "response": "",
            "user_constraints": "",
            "key_data": {},
            "budget_exempt": None,
            "user_approved_overrun_count": 0,
            "step_results": [],
            "observer_log": [],
            "template_id": template_id,
            "selected_skills": selected_skills,
            "tool_calls": [],
        }

    def _get_recursion_limit(self) -> int:
        """递归限制"""
        return 50

    def _extract_completed_steps(self, last_state) -> list[tuple[str, str]]:
        """从 last_state 提取已完成步骤 [(desc, result), ...]"""
        return last_state.get("past_steps", [])

    # ── 模板方法 ──

    def _create_budget(self):
        """创建预算控制器。子类可覆盖以使用不同的预算限制。"""
        return BudgetControllerFactory.create()

    async def run(self, user_input: str, registry, memory, logger, progress_callback=None, **kwargs):
        logger = ensure_radar(logger)
        # 模板路由用原始输入（不含画像/记忆污染），避免历史查询关键词干扰匹配
        template_id, selected_skills = select_and_load_template(user_input, logger, history=memory.get_history() if memory else None)
        original_user_input = user_input  # 保留原始输入，避免丰富后的文本污染记忆和用户画像
        user_input = self._enrich_user_input(
            user_input, progress_callback=progress_callback,
            history=memory.get_history() if memory else None,
        )

        async with AgentRunContext(self.name, logger, user_input) as run_ctx:
            budget = self._create_budget()
            run_ctx.budget = budget
            checkpointer = create_checkpointer()

            app = self._build_graph(
                logger=logger,
                memory=memory,
                registry=registry,
                progress_callback=progress_callback,
                budget=budget,
                checkpointer=checkpointer,
            )

            config = {
                "configurable": {"thread_id": run_ctx.run_id},
                "recursion_limit": self._get_recursion_limit(),
                "callbacks": run_ctx.build_callbacks(self._build_callbacks_tag()),
            }

            inputs = self._build_initial_state(user_input, template_id, selected_skills, **kwargs)
            last_state = inputs.copy()
            full_response = ""

            # 用户交互回调：捕获 last_state 以展示进度
            def budget_exceeded_callback(last_state, logger, error):
                return self._handle_budget_exceeded(last_state, logger, error)

            try:
                full_response = await stream_and_collect(app, inputs, config, last_state)
                run_ctx.end_trace(full_response, status="success")
            except Exception as e:
                handler_ctx = PlanHandlerContext(
                    app=app,
                    config=config,
                    last_state=last_state,
                    extract_completed_steps=lambda: self._extract_completed_steps(last_state),
                    user_input=user_input,
                    exec_state=None,
                    llm=None,
                    logger=logger,
                    budget=budget,
                    graph=app,
                    run_ctx=run_ctx,
                    handle_budget_exceeded_callback=budget_exceeded_callback,
                    thread_id=run_ctx.run_id,
                )
                full_response = await self._exception_handlers.handle(e, handler_ctx)

        # 图执行完成后写入 memory，使用原始输入（非丰富后文本），避免历史污染
        memory.add_user(original_user_input)
        self._update_intent_memory(original_user_input, full_response, logger)
        return full_response

    def _handle_budget_exceeded(self, last_state, logger, error) -> str:
        """用户交互回调 — 询问用户是否继续

        返回 "" 表示继续（checkpoint 恢复），返回非空字符串表示停止并生成总结。
        """
        from agents.shared.exception_utils import handle_budget_exceeded

        logger.warning("B", f"预算超限: {error}")
        decision = handle_budget_exceeded(last_state, logger)

        if decision.get("_user_approved_overrun"):
            return ""  # 空字符串 = 继续（checkpoint 恢复）
        return decision.get("response", f"预算超限({error})")

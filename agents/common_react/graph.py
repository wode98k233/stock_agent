"""子 ReAct 图构建与执行

非序列化对象（budget/exec_state/logger/tools）通过节点闭包传递，
不存入 ReactSubState，确保 state 可被 LangGraph checkpoint 序列化。
"""
from langgraph.graph import StateGraph, START, END

from agents.common_react.state import ReactSubState
from agents.common_react.nodes import (
    SubAgentNode,
    build_sub_tool_node,
    should_continue,
    extract_final_result,
)
from agents.executor_callbacks import ExecutionState, ExecutionStateCallback
from utils.token_recorder import TokenRecorder, MetricsBackend, BudgetBackend
from utils.budget import BudgetExceeded, BudgetExemptWindow
from utils.logger import ensure_radar
from config import Config


class ReactSubGraph:
    """子 ReAct 图（单步执行器）

    拓扑：
    START → agent → [should_continue] → tools → agent (循环)
                       └→ END

    非序列化对象通过 __init__ 闭包捕获，不存入 state。
    支持 checkpointer 实现 checkpoint 恢复。
    """

    def __init__(self, llm, max_iterations=None, logger=None,
                 budget=None, exec_state=None, tools=None, checkpointer=None,
                 loop_threshold=2, template_id=None, rate_limiter=None):
        self.llm = llm
        self.max_iterations = max_iterations or Config.PLAN_EXECUTOR_TOOL_CALLS
        self.logger = ensure_radar(logger)
        self.budget = budget
        self.exec_state = exec_state
        self.tools = tools or []
        self.checkpointer = checkpointer
        self.loop_threshold = loop_threshold
        self.template_id = template_id
        self._user_goal = {"goal": ""}  # 可变容器，ainvoke 时更新
        self._rate_limiter = rate_limiter
        self._graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(ReactSubState)

        # 闭包注入依赖到节点（不通过 state 传递）
        agent_node = SubAgentNode(
            self.llm, budget=self.budget, exec_state=self.exec_state,
            tools=self.tools, logger=self.logger, loop_threshold=self.loop_threshold,
        )
        tool_node = build_sub_tool_node(
            exec_state=self.exec_state, tools=self.tools, logger=self.logger,
            user_goal_ref=self._user_goal,
            rate_limiter=self._rate_limiter,
        )

        workflow.add_node("agent", agent_node)
        workflow.add_node("tools", tool_node)

        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", "end": END},
        )
        workflow.add_edge("tools", "agent")

        return workflow.compile(checkpointer=self.checkpointer)

    async def ainvoke(
        self,
        step_purpose: str | None = None,
        context_messages: list = None,
        tools: list = None,
        budget=None,
        exec_state: ExecutionState = None,
        failed_tools: list = None,
        callbacks: list = None,
        metadata: dict = None,
        config: dict = None,
        template_id: str | None = None,
    ) -> dict:
        """执行子 ReAct 图，返回 {tool_results, final_result}

        step_purpose=None 时从 checkpoint 恢复（不传 initial_state）。
        """
        if exec_state is None:
            exec_state = self.exec_state or ExecutionState()

        invoke_config = dict(config or {})
        invoke_config.setdefault("recursion_limit", self.max_iterations * 2 + 10)

        _callbacks = self._build_callbacks(budget or self.budget, exec_state, callbacks)
        if _callbacks:
            invoke_config["callbacks"] = _callbacks
        if metadata:
            invoke_config["metadata"] = metadata

        # 更新用户目标供压缩器使用
        if step_purpose:
            self._user_goal["goal"] = step_purpose

        final_state = None
        try:
            if step_purpose is None:
                # 从 checkpoint 恢复：不传 initial_state，LangGraph 自动加载上次 checkpoint
                self.logger.info("R", "子 ReAct Graph 从 checkpoint 恢复执行")
                final_state = await self._graph.ainvoke(None, config=invoke_config)
                final_result = final_state.get("final_result") or extract_final_result(final_state)
            else:
                # 正常执行：构建 initial_state
                system_prompt = (
                    f"执行以下任务：{step_purpose}\n\n"
                    "执行规则：\n"
                    "1. 同一工具调用失败后，不要用相同参数重试\n"
                    "2. 如果某个工具不可用，尝试用其他工具获取类似信息\n"
                    "3. 最多调用 {max_iter} 次工具，达到上限后必须生成总结\n\n"
                    "【目标达成判定 — 严格执行】\n"
                    "- 每次工具调用返回结果后，立即判断：本次结果是否已经包含任务所需的数据？\n"
                    "- 如果是，**立刻停止调用工具，直接生成总结**。不得以\"更全面\"、\"交叉验证\"、\"补充数据\"为由继续调用\n"
                    "- 例如：任务是\"获取收盘价\"，第一次调用返回了收盘价 → 目标达成，停止\n"
                    "- 例如：任务是\"获取一条新闻\"，第一次调用返回了一条新闻 → 目标达成，停止\n"
                    "- 用不同参数查同一类数据（如换日期范围、换表述方式）不算新信息，属于无效重复\n"
                    "- 如果连续 2 次工具调用返回的数据与已有所得实质相同，必须立即总结"
                ).format(max_iter=self.max_iterations)
                if failed_tools:
                    system_prompt += f"\n6. 以下工具已尝试失败，请勿重试：{', '.join(failed_tools)}"

                # 注入模板指引（与 React 主循环对齐）
                _tid = template_id or self.template_id
                if _tid and getattr(Config, "REPORT_ENABLE_ANALYSIS_ENGINE", True):
                    try:
                        from agents.analysis.template_store import load_template, build_guidance
                        template = load_template(_tid)
                        guidance = build_guidance(template, step_purpose or "")
                        if guidance:
                            system_prompt += guidance
                    except Exception:
                        pass

                exec_messages = [
                    ("system", system_prompt),
                    *(context_messages or []),
                ]

                initial_state: ReactSubState = {
                    "messages": exec_messages,
                    "iteration_count": 0,
                    "max_iterations": self.max_iterations,
                    "tool_calls_count": 0,
                    "final_result": None,
                    "should_stop": False,
                    "step_purpose": step_purpose,
                    "all_tool_names": [t.name for t in self.tools],
                    "failed_tools": list(failed_tools or []),
                    "cache_hit_tools": [],
                }

                final_state = await self._graph.ainvoke(initial_state, config=invoke_config)
                final_result = final_state.get("final_result") or extract_final_result(final_state)
        except BudgetExceeded:
            # 预算超限是正常终止路径，基于已有结果生成总结
            if exec_state.has_useful_results():
                final_result = exec_state.get_summary_context()
            else:
                raise
        except Exception:
            # 其他异常：有部分结果则生成总结，否则上抛
            if exec_state.has_useful_results():
                final_result = exec_state.get_summary_context()
            else:
                raise

        # 收集工具结果：直接从 exec_state.tool_calls 读取。
        # tool_node_base._run_one 的 name+input 匹配保证 tool_output 是压缩后的版本。
        tool_results = []
        for tc in exec_state.tool_calls:
            tool_results.append({
                "tool": tc.tool_name,
                "input": tc.tool_input,
                "output": tc.tool_output or "",
            })

        return {
            "tool_results": tool_results,
            "final_result": final_result,
        }

    def _build_callbacks(self, budget, exec_state, extra_callbacks=None):
        """组装 callbacks 列表"""
        _callbacks = list(extra_callbacks or [])
        if self.logger:
            backends = [MetricsBackend()]
            if budget:
                backends.append(BudgetBackend(budget))
            _callbacks.append(TokenRecorder(self.logger, "react-sub", backends))
        if exec_state:
            _callbacks.append(ExecutionStateCallback(exec_state, self.logger))
        return _callbacks


async def run_react_subgraph(
    llm,
    tools: list,
    step_purpose: str,
    context_messages: list,
    max_iterations: int = None,
    callbacks: list = None,
    budget=None,
    logger=None,
    metadata: dict = None,
    failed_tools: list = None,
    checkpointer=None,
    thread_id: str = None,
    handle_budget_callback=None,
    template_id: str = None,
    rate_limiter=None,
) -> dict:
    """执行 ReAct 子图（便捷函数）。

    支持 checkpoint 恢复：当 BudgetExceeded 时，如果 handle_budget_callback 返回
    用户确认继续，则设置豁免窗口并通过 ainvoke(None) 从 checkpoint 断点续跑。

    返回：
    {
        "tool_results": [{"tool": "...", "input": ..., "output": ...}, ...],
        "final_result": "结构化总结",
    }
    """
    exec_state = ExecutionState()

    graph = ReactSubGraph(
        llm=llm,
        max_iterations=max_iterations,
        logger=logger,
        budget=budget,
        exec_state=exec_state,
        tools=tools,
        checkpointer=checkpointer,
        template_id=template_id,
        rate_limiter=rate_limiter,
    )

    config = {}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    try:
        return await graph.ainvoke(
            step_purpose=step_purpose,
            context_messages=context_messages,
            budget=budget,
            exec_state=exec_state,
            failed_tools=failed_tools,
            callbacks=callbacks,
            metadata=metadata,
            config=config,
        )
    except BudgetExceeded:
        # 子图内预算超限：尝试 checkpoint 恢复
        if handle_budget_callback and checkpointer and thread_id:
            decision = handle_budget_callback()
            if decision.get("_user_approved_overrun"):
                exempt = BudgetExemptWindow(
                    calls_limit=Config.BUDGET_EXEMPT_CALLS_LIMIT,
                    time_limit=Config.BUDGET_EXEMPT_TIME_LIMIT,
                )
                budget.set_exempt_window(exempt)
                logger.info("B", "子图 checkpoint 恢复，创建豁免窗口")
                exec_state._checkpoint_restored = True

                return await graph.ainvoke(
                    step_purpose=None,  # 从 checkpoint 恢复
                    callbacks=callbacks,
                    metadata=metadata,
                    config=config,
                )

        # 降级：有结果则总结
        if exec_state.has_useful_results():
            return {
                "tool_results": [
                    {"tool": tc.tool_name, "input": tc.tool_input, "output": tc.tool_output or ""}
                    for tc in exec_state.tool_calls
                ],
                "final_result": exec_state.get_summary_context(),
            }
        raise
    except Exception:
        # 其他异常：有结果则降级，否则上抛
        if exec_state.has_useful_results():
            return {
                "tool_results": [
                    {"tool": tc.tool_name, "input": tc.tool_input, "output": tc.tool_output or ""}
                    for tc in exec_state.tool_calls
                ],
                "final_result": exec_state.get_summary_context(),
            }
        raise

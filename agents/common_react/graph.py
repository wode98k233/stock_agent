"""子 ReAct 图构建与执行

非序列化对象（budget/exec_state/logger/tools）通过节点闭包传递，
不存入 ReactSubState，确保 state 可被 LangGraph checkpoint 序列化。
"""
from langgraph.graph import StateGraph, START, END

from agents.common_react.state import ReactSubState
from agents.prompts import REACT_SYSTEM_PROMPT
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
                 loop_threshold=2, template_id=None, rate_limiter=None,
                 tail_system: str = ""):
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
        self._tail_system = tail_system
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
        guidance_input: str | None = None,
    ) -> dict:
        """执行子 ReAct 图，返回 {tool_results, final_result}

        step_purpose=None 时从 checkpoint 恢复（不传 initial_state）。

        :param guidance_input: 计算模板 guidance 时用的 user_input；None → 用 ``step_purpose``。
            unified 传 ``state["input"]``，与 planner 用同一个串 → 两处 guidance 逐字节相同。
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
                    "1. 同一工具调用失败后，不要用相同参数重试；但若失败为限流/超时类（错误含 122、429、操作过于频繁），等待几秒后换表述重试 1 次\n"
                    "2. 如果某个工具不可用，尝试用其他工具获取类似信息\n"
                    "3. 若关键数据因接口失败缺失，在剩余预算内允许补查 1-2 次（换关键词/换工具），不要缺着数据提前收尾\n"
                    "4. 最多调用 {max_iter} 次工具，达到上限后必须生成总结\n\n"
                    "【目标达成判定 — 严格执行】\n"
                    "- 每次工具调用返回结果后，立即判断：本次结果是否已经包含任务所需的数据？\n"
                    "- 如果是，**立刻停止调用工具，直接生成总结**。不得以\"更全面\"、\"交叉验证\"、\"补充数据\"为由继续调用\n"
                    "- 例如：任务是\"获取收盘价\"，第一次调用返回了收盘价 → 目标达成，停止\n"
                    "- 例如：任务是\"获取一条新闻\"，第一次调用返回了一条新闻 → 目标达成，停止\n"
                    "- 用不同参数查同一类数据（如换日期范围、换表述方式）不算新信息，属于无效重复\n"
                    "- 如果连续 2 次工具调用返回的数据与已有所得实质相同，必须立即总结"
                ).format(max_iter=self.max_iterations)
                if failed_tools:
                    system_prompt += f"\n5. 以下工具已尝试失败，请勿重试：{', '.join(failed_tools)}"

                # 注入模板指引（与 React 主循环对齐）
                # 注意：这里是**子步执行器**（group 子 Agent / pdor / plan / unified 共用本函数），
                # 只负责一个维度，因此传 include_report_structure=False 剔除报告级约束
                # （报告输出结构 + 质量门禁，实测占指引 72%）。
                # 2026-10-01 事故：报告结构泄漏导致每个子 Agent 都写了一份完整 10 段报告。
                _tid = template_id or self.template_id
                # ⚠️ 2026-10-17 实测结论：guidance **必须留在 user 之前**，不能挪尾部。
                # 曾尝试把它挪到尾部以便 planner 与之共享 `[REACT][history][user]`，
                # 但那个可共享头只有 ~700 tok < provider 的 2048 分块 → 一个块都缓存不到；
                # 而 guidance 留在头部时，planner 自己那段静态 prompt 才是天然可缓存头
                # （跨 run 稳定）。A/B 实测：挪尾后 unified planner 命中 41%→2%、
                # 整 run 67.8%→60.2%，净负。故保持原位。
                if _tid and getattr(Config, "REPORT_ENABLE_ANALYSIS_ENGINE", True):
                    try:
                        from agents.analysis.template_store import load_template, build_guidance
                        template = load_template(_tid)
                        guidance = build_guidance(
                            template,
                            guidance_input if guidance_input is not None else (step_purpose or ""),
                            include_report_structure=False)
                        _boundary = (
                            "\n\n## 你的输出边界（重要）\n"
                            "你是子 Agent，只负责上述这一个维度。禁止输出完整报告结构；"
                            "禁止编写「仓位与风险预算 / 交易纪律 / 五类投资者适配 / 行动方案 / "
                            "小白版翻译」等报告级板块——这些由报告节点统一完成。"
                            "只输出本维度的数据与判断。"
                        )
                        if guidance and self._tail_system:
                            # guidance 留在 user **之前**（与 React 主链
                            # [REACT][history][guidance][user] 同形）；
                            # 「输出边界」属任务约束，随任务指令一起去尾部。
                            _cm = list(context_messages or [])
                            _tail_user = None
                            if _cm and _cm[-1][0] == "user":
                                _tail_user = _cm.pop()
                            _cm.append(("system", guidance))
                            if _tail_user is not None:
                                _cm.append(_tail_user)
                            context_messages = _cm
                            self._tail_system = f"{self._tail_system}{_boundary}"
                        elif guidance:
                            system_prompt += guidance + _boundary
                    except Exception:
                        pass

                # ── 前缀形态对齐主链（PRD-A11 FR-10/11）────────────────────
                # 旧形态：[system: 执行以下任务…+规则+guidance+边界][history][user]
                #         → 第一条消息是任务专属的，**与主链的 [REACT] 完全不同**，
                #           于是内层 agent 只能命中 tools+cache_prefix（实测 37%）。
                #
                # 新形态：[REACT system][history][guidance'][user][尾部: 任务+规则+边界]
                #         —— 与 React 主链的 prepare / select_skills 同形，前缀可共享；
                #           任务指令移到尾部（模型照样能读到，位置不影响它被遵循）。
                # 未传 tail_system 时（其它调用方）保持旧行为，零回归。
                if self._tail_system:
                    # 尾部 = 任务+执行规则（system_prompt） + guidance/边界/技能详情/约束。
                    #
                    # ⚠️ system_prompt 必须显式并入：旧写法 `("system", self._tail_system)`
                    # 把 system_prompt 整段丢了 —— 内层子图拿不到「执行以下任务：{step_purpose}」，
                    # 也拿不到「目标达成判定」等 5 条执行规则（2026-10-17 探针实测 payload 里
                    # 两条都不存在），是 cdf5ae5 引入的回归。任务在尾部不影响模型遵循，
                    # 但会破坏"目标达成即停"，导致过度调用工具。
                    exec_messages = [
                        ("system", REACT_SYSTEM_PROMPT),
                        *(context_messages or []),
                        ("system", f"{system_prompt}\n\n{self._tail_system}"),
                    ]
                else:
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
            # PRD-A9 §14 fork：把内层对话与同一份工具数组交出去，供下游报告复用
            # 已热前缀（unified_executor 只需透传即可，无需重建）。非对话类状态，
            # 不进 LangGraph checkpoint 的可序列化路径。
            # ⚠️ 必须用 exec_state.last_llm_messages（BaseAgentNode 在每次 LLM 调用前
            # 记录「实际发出的消息」），不能用 final_state["messages"] —— 后者是未经
            # _prepare_messages 处理的 state 原始版：上下文压缩器 _merge_leading_system
            # 会把头部连续 system 合并为 1 条（7 条 → 1 条，JSON 骨架差 ~162B），
            # 两者前缀指纹必然不同 → fork 整段前缀失效，report-fast 命中率跌到个位数
            # （2026-10-15 实测：unified 7%、e2e 13.3%；react 主图同款 last_llm 机制 92%+）。
            # 空值（LLM 未调用就退出）→ 下游回落证据注入路径，绝不拿未对齐前缀硬发。
            "_fork_messages": list(getattr(exec_state, "last_llm_messages", None) or []),
            "_fork_tools": list(getattr(exec_state, "last_llm_tools", None) or []),
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
    tail_system: str = "",
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
    guidance_input: str = None,
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
        tail_system=tail_system,
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
            guidance_input=guidance_input,
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

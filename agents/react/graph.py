"""ReAct Agent Graph 构建

9 节点完整流程图：
    START → classify → [_route_after_classify]
      ├── "end"            → END（非股票问题）
      └── "select_template" → select_template → select_skills → prepare → agent
                                   → [_route_after_agent]
                                     ├── "tools"         → tool_node → agent (循环)
                                     ├── "force_summary" → partial_summary → END
                                     ├── "report"        → template_report → dashboard → END
                                     └── "end"           → END

设计原则：
- Node 关注逻辑：每个节点是纯计算/IO 单元
- DIContainer 关注依赖：统一管理节点创建和共享状态
- Graph 关注条件边：图的定义只包含节点注册和条件路由
- Agent 关注入口与异常：创建上下文、调用图、处理异常
- budget、exec_state、logger 通过节点闭包传递，不存 state（不可序列化）
"""
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END

from agents.react.state import ReactAgentState
from agents.react.di_container import ReactDIContainer
from agents.react.nodes import (
    _route_after_classify,
    make_route_after_agent,
    extract_final_result,
)
from agents.executor_callbacks import ExecutionState
from utils.budget import BudgetController
from utils.llm_factory import TokenCompatibleChatOpenAI
from utils.logger import ensure_radar
from config import Config


class ReactGraph:
    """自定义 ReAct Graph

    9 节点完整流程图，覆盖从分类到报告的全流程。
    节点创建委托给 ReactDIContainer，Graph 只负责图结构定义。
    budget/exec_state/logger 通过节点闭包传递，不存入 state。
    """

    def __init__(
        self,
        llm: TokenCompatibleChatOpenAI,
        max_iterations: int = 20,
        logger=None,
        skill_registry=None,
        memory=None,
        budget: BudgetController = None,
        exec_state: ExecutionState = None,
        checkpointer=None,
    ):
        self.llm = llm
        self.max_iterations = max_iterations
        self.logger = ensure_radar(logger)
        self.budget = budget
        self.exec_state = exec_state
        self.checkpointer = checkpointer

        # 依赖注入容器：统一管理节点创建和共享状态（tools_ref）
        self._di = ReactDIContainer(
            llm=llm,
            memory=memory,
            skill_registry=skill_registry,
            budget=budget,
            exec_state=exec_state,
            logger=logger,
        )

        self._graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(ReactAgentState)

        # 通过 DI 容器创建所有节点，tools_ref 由容器统一持有
        nodes = self._di.create_all_nodes()
        for name, node in nodes.items():
            workflow.add_node(name, node)

        workflow.add_edge(START, "classify")

        workflow.add_conditional_edges(
            "classify",
            _route_after_classify,
            {"select_template": "select_template", "end": END},
        )

        workflow.add_edge("select_template", "select_skills")
        workflow.add_edge("select_skills", "prepare")
        workflow.add_edge("prepare", "agent")

        route_after_agent = make_route_after_agent(self.exec_state)
        workflow.add_conditional_edges(
            "agent",
            route_after_agent,
            {
                "tools": "tools",
                "force_summary": "partial_summary",
                "report": "template_report",
                "end": END,
            },
        )

        workflow.add_edge("tools", "agent")

        workflow.add_edge("partial_summary", END)
        workflow.add_edge("template_report", "dashboard")
        workflow.add_edge("dashboard", END)

        return workflow.compile(checkpointer=self.checkpointer)

    async def ainvoke(
        self,
        user_input: str | None,
        config: RunnableConfig | None = None,
    ) -> dict:
        if user_input is None:
            # 从 checkpoint 恢复：不传 initial_state，LangGraph 自动加载上次 checkpoint
            invoke_config = dict(config or {})
            invoke_config.setdefault("recursion_limit", self.max_iterations * 2 + 10)
            self.logger.info("R", f"ReAct Graph 从 checkpoint 恢复执行，最大迭代 {self.max_iterations}")
            final_state = await self._graph.ainvoke(None, config=invoke_config)
        else:
            initial_state: ReactAgentState = {
                "messages": [],
                "iteration_count": 0,
                "max_iterations": self.max_iterations,
                "tool_calls_count": 0,
                "final_result": None,
                "should_stop": False,
                "user_input": user_input,
                "is_stock_related": True,
                "classify_response": None,
                "selected_template_id": Config.REPORT_TEMPLATE,
                "selected_template": None,
                "selected_skills": [],
                "all_tool_names": [],
                "limit_reason": None,
                "cache_hit_tools": [],
            }

            invoke_config = dict(config or {})
            invoke_config.setdefault("recursion_limit", self.max_iterations * 2 + 10)

            self.logger.info("R", f"ReAct Graph 开始执行，最大迭代 {self.max_iterations}")

            final_state = await self._graph.ainvoke(initial_state, config=invoke_config)

        final_result = final_state.get("final_result") or extract_final_result(final_state)

        self.logger.info(
            "R",
            f"ReAct Graph 执行完成，迭代 {final_state['iteration_count']}/{self.max_iterations}，"
            f"工具调用 {final_state['tool_calls_count']} 次",
        )

        return {
            "messages": final_state["messages"],
            "final_result": final_result,
            "iteration_count": final_state["iteration_count"],
            "tool_calls_count": final_state["tool_calls_count"],
            "is_stock_related": final_state.get("is_stock_related", True),
            "classify_response": final_state.get("classify_response"),
            "limit_reason": final_state.get("limit_reason"),
        }


def create_react_graph(
    llm: TokenCompatibleChatOpenAI,
    max_iterations: int = 20,
    logger=None,
    skill_registry=None,
    memory=None,
    budget=None,
    exec_state=None,
    checkpointer=None,
) -> ReactGraph:
    return ReactGraph(
        llm=llm,
        max_iterations=max_iterations,
        logger=logger,
        skill_registry=skill_registry,
        memory=memory,
        budget=budget,
        exec_state=exec_state,
        checkpointer=checkpointer,
    )
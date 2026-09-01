"""ReAct Agent 依赖注入容器。

职责单一：集中管理所有共享依赖和节点工厂方法，
将 ReactGraph 从节点创建/依赖协调的负担中解放出来。

设计要点：
- 共享依赖（llm、memory、budget 等）在容器初始化时注入
- tools_ref 由容器统一持有，SelectSkillsNode 写入后后续节点自动可见
- 每个节点有对应的 create_xxx 工厂方法，签名与节点构造函数对齐
- 容器本身无状态变更逻辑，只做对象组装
"""
from __future__ import annotations

from typing import Optional

from utils.logger import ensure_radar


class ReactDIContainer:
    """ReAct Agent 依赖注入容器。

    持有所有共享依赖和 tools_ref，提供统一的节点工厂方法。
    """

    def __init__(
        self,
        llm,
        memory,
        skill_registry,
        budget,
        exec_state,
        logger=None,
    ):
        self._llm = llm
        self._memory = memory
        self._skill_registry = skill_registry
        self._budget = budget
        self._exec_state = exec_state
        self._logger = ensure_radar(logger)

        # 共享工具引用：SelectSkillsNode 写入，后续节点通过闭包读取
        # 不可序列化对象不存入 state，避免 checkpoint msgpack 失败
        self._tools_ref: list = []

    # ──── 共享状态访问 ────

    @property
    def tools_ref(self) -> list:
        """供 ReactGraph 条件边函数等外部访问。"""
        return self._tools_ref

    @property
    def logger(self):
        return self._logger

    # ──── 节点工厂方法 ────

    def create_classify_node(self):
        from agents.react.nodes import ClassifyNode
        return ClassifyNode(
            llm=self._llm,
            memory=self._memory,
            budget=self._budget,
            logger=self._logger,
        )

    def create_select_template_node(self):
        from agents.react.nodes import SelectTemplateNode
        return SelectTemplateNode(memory=self._memory, logger=self._logger)

    def create_select_skills_node(self):
        from agents.react.nodes import SelectSkillsNode
        return SelectSkillsNode(
            llm=self._llm,
            skill_registry=self._skill_registry,
            memory=self._memory,
            budget=self._budget,
            tools_ref=self._tools_ref,
            logger=self._logger,
        )

    def create_prepare_node(self):
        from agents.react.nodes import PrepareNode
        return PrepareNode(
            memory=self._memory,
            skill_registry=self._skill_registry,
            tools=self._tools_ref,
            logger=self._logger,
        )

    def create_agent_node(self):
        from agents.react.nodes import AgentNode
        return AgentNode(
            llm=self._llm,
            budget=self._budget,
            exec_state=self._exec_state,
            tools=self._tools_ref,
            logger=self._logger,
        )

    def create_tool_node(self):
        from agents.react.nodes import ToolNode
        return ToolNode(
            exec_state=self._exec_state,
            tools=self._tools_ref,
            logger=self._logger,
        )

    def create_partial_summary_node(self):
        from agents.react.nodes import PartialSummaryNode
        return PartialSummaryNode(
            llm=self._llm,
            exec_state=self._exec_state,
            memory=self._memory,
            logger=self._logger,
        )

    def create_template_report_node(self):
        from agents.react.nodes import TemplateReportNode
        return TemplateReportNode(
            exec_state=self._exec_state,
            budget=self._budget,
            logger=self._logger,
        )

    def create_dashboard_node(self):
        from agents.react.nodes import DashboardNode
        return DashboardNode(
            exec_state=self._exec_state,
            budget=self._budget,
        )

    def create_all_nodes(self) -> dict:
        """一次性创建所有节点，返回 {name: node} 字典。"""
        return {
            "classify": self.create_classify_node(),
            "select_template": self.create_select_template_node(),
            "select_skills": self.create_select_skills_node(),
            "prepare": self.create_prepare_node(),
            "agent": self.create_agent_node(),
            "tools": self.create_tool_node(),
            "partial_summary": self.create_partial_summary_node(),
            "template_report": self.create_template_report_node(),
            "dashboard": self.create_dashboard_node(),
        }

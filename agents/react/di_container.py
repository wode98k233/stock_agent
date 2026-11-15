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

        # 共享工具引用：SelectSkillsNode / SubGoalRouterNode / SkillGapNode 写入，
        # 后续节点通过闭包读取；不可序列化对象不存入 state，避免 checkpoint msgpack 失败
        self._tools_ref: list = []
        from agents.shared.tool_utils import activate_tool_mode
        self._activate_mode = activate_tool_mode()
        self._full_surface = self._maybe_load_full_surface()

    def _maybe_load_full_surface(self) -> bool:
        """全量常驻形态：在此**一次性**装载全部工具，之后任何节点都不得改写。

        tools 数组是 prompt 前缀的位置 0，缓存只对连续前缀生效。让它每轮随
        技能批次 clear()+extend()，等于每次都把前缀作废 —— 这是命中率最大的
        结构性损失。代价是每次请求固定多带约 1.7K~2K token 的工具 schema，
        但这部分一旦命中就是全价折扣，远优于整条前缀作废。
        """
        from agents.shared.tool_utils import activate_tool_mode, is_full_surface
        if not is_full_surface():
            return False
        try:
            tools = self._skill_registry.get_all_tools()
        except Exception as e:
            self._logger.warning("R", f"全量工具装载失败，回落 strict 工具面: {e}")
            return False
        if not tools:
            self._logger.warning("R", "全量工具为空，回落 strict 工具面")
            return False
        self._tools_ref.extend(tools)
        self._logger.info("R", f"全量常驻工具面: 一次装载 {len(tools)} 个工具，"
                               f"后续切技能只追加 usage_guide、不再换绑")

        # model 形态：模型自己决定激活哪个技能，需要一个能查全量目录的元操作工具
        if activate_tool_mode() == "model":
            try:
                from agents.react.activate_skill_tool import build_activate_skill_tool
                self._tools_ref.append(
                    build_activate_skill_tool(self._skill_registry, self._logger))
                self._logger.info("R", "model 投递方式: 已挂载 activate_skill 元操作工具")
            except Exception as e:
                self._logger.warning("R", f"activate_skill 挂载失败，"
                                         f"退回 router 投递: {e}")
        return True

    @property
    def full_surface(self) -> bool:
        """当前是否为全量常驻工具面（供节点决定投递 usage_guide 还是换绑）。"""
        return self._full_surface

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

    def create_sub_goal_router_node(self):
        from agents.react.skill_nodes import SubGoalRouterNode
        return SubGoalRouterNode(llm=self._llm, skill_registry=self._skill_registry,
                                 tools_ref=self._tools_ref, budget=self._budget, logger=self._logger,
                                 allow_catalog_fallback=True,
                                 full_surface=self._full_surface,
                                 activate_mode=self._activate_mode)

    def create_skill_gap_node(self):
        from agents.react.skill_nodes import SkillGapNode
        return SkillGapNode(llm=self._llm, skill_registry=self._skill_registry,
                            tools_ref=self._tools_ref, budget=self._budget, logger=self._logger,
                            full_surface=self._full_surface,
                                 activate_mode=self._activate_mode)

    def create_reselect_node(self):
        """中途换批：复用 router 逻辑，但 append_usage=True（补新增技能的 L1 用法）。"""
        from agents.react.skill_nodes import SubGoalRouterNode
        return SubGoalRouterNode(llm=self._llm, skill_registry=self._skill_registry,
                                 tools_ref=self._tools_ref, budget=self._budget,
                                 logger=self._logger, append_usage=True,
                                 allow_catalog_fallback=True,
                                 full_surface=self._full_surface,
                                 activate_mode=self._activate_mode)

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
            skill_registry=self._skill_registry,
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
            # PRD-A9 §14 fork：报告要绑定与 AgentNode 完全同一份工具数组，
            # 位置 0 才能与 agent 的已热前缀逐字节对齐。
            tools_ref=self._tools_ref,
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
            "sub_goal_router": self.create_sub_goal_router_node(),
            "skill_gap": self.create_skill_gap_node(),
            "reselect": self.create_reselect_node(),
            "prepare": self.create_prepare_node(),
            "agent": self.create_agent_node(),
            "tools": self.create_tool_node(),
            "partial_summary": self.create_partial_summary_node(),
            "template_report": self.create_template_report_node(),
            "dashboard": self.create_dashboard_node(),
        }

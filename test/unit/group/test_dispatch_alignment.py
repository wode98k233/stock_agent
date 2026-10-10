"""
测试目标: Agent Group 调度-计划对齐护栏（2026-10-01 recursion_limit=60 空转事故）
覆盖范围:
  - A supervisor_node: 调度槽位重定位（重复调度/计划外/正常对齐/partial 复用）
  - D supervisor_node: 计划外 agent 一次性放行 + 重复调度剔除 + 全剔除 early_stop
  - B subagents/base.py: 槽位归属校验、计划外执行分支、越界标记
  - C dispatcher.py: 匹配放宽（partial 复用）、槽位回溯、计划外放行、空转护栏 early_stop
  - _route_after_parallel: early_stop → report 路由

事故背景：Supervisor 重复调度 data_collector 补采时，子 Agent 盲用 current_step_index
把"分析师并行步骤"槽位覆盖成自己的结果并推进越界 → parallel_executor 永远匹配不到
步骤 → Supervisor↔PE 空转 ~27 轮打满 recursion_limit=60，烧 68.8 万 token，0 步骤成功。
"""
import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# mock 掉 langgraph 依赖链（与 test_subagents_base.py 同策略）
for mod in ["langgraph", "langgraph.graph", "langgraph.graph.state", "langgraph.graph.message",
            "langgraph.cache", "langgraph.cache.base", "langgraph.checkpoint",
            "langgraph.checkpoint.serde", "langgraph.checkpoint.serde.jsonplus",
            "langgraph_checkpoint", "langgraph_checkpoint.serde"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from agents.group.dispatcher import _route_after_parallel, parallel_executor_node


# ── 共享 helpers ─────────────────────────────────────────────

def _step(number, agents, purpose="test", status="pending", step_id=None):
    return {
        "step": step_id if step_id is not None else number,
        "agent_names": agents,
        "task_purpose": purpose,
        "input_params": {},
        "status": status,
        "result": "",
        "feedback": "",
        "requests": [],
        "retry_count": 0,
        "executed_at": "",
        "run_group": 1,
        "depends_on": [],
    }


def _make_state(**overrides):
    base = {
        "input": "给我市场的9月总结以及10月展望",
        "plan_steps": [],
        "current_step_index": 0,
        "original_plan": [],
        "accumulated_data": "",
        "resolved_data": "",
        "constraints": "",
        "observation": "",
        "response": "",
        "budget_exempt": None,
        "_replan_count": 0,
        "_failed_agents": None,
        "_error_message": None,
        "template_id": None,
        "tool_calls": [],
        "selected_skills": [],
        "dialog_uuid": "test-uuid",
        "task_id": "test-task",
        "current_agent_names": [],
        "current_task_purpose": "",
        "dispatch_history": [],
        "_extra_dispatched": [],
        "_extra_executed": [],
        "_empty_dispatch_count": 0,
    }
    base.update(overrides)
    return base


def _make_ctx():
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.budget = MagicMock()
    ctx.budget._llm = MagicMock()
    ctx.progress_reporter = MagicMock()
    ctx.memory = MagicMock()
    ctx.memory.enabled = False
    return ctx


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


# ── A: supervisor 调度槽位重定位 ─────────────────────────────

def _accident_plan():
    """事故现场的 2 步计划：步骤 2 是分析师并行槽位"""
    return [
        _step(1, ["data_collector"], "采集数据底版", status="partial"),
        _step(2, ["macro_analyst", "chain_analyst", "risk_analyst"], "并行分析", status="pending"),
    ]


class TestSupervisorRelocation:
    """A: supervisor_node 调度后重定位 current_step_index"""

    @pytest.mark.asyncio
    async def test_repeated_dispatch_relocates_to_partial_step(self):
        """事故核心场景：补采调度 data_collector → 槽位重定位回步骤 1（partial 可复用）"""
        from agents.group.nodes.supervisor import supervisor_node

        state = _make_state(
            plan_steps=_accident_plan(),
            current_step_index=1,  # 已漂移到步骤 2
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "data_collector", "purpose": "补采缺口数据"}
            result = await supervisor_node(state, ctx)

        assert result["current_agent_names"] == ["data_collector"]
        assert result["current_step_index"] == 0  # 重定位回 data_collector 自己的槽位

    @pytest.mark.asyncio
    async def test_aligned_dispatch_keeps_index(self):
        """调度 agent 与当前槽位对齐 → 不额外写 current_step_index"""
        from agents.group.nodes.supervisor import supervisor_node

        state = _make_state(
            plan_steps=_accident_plan(),
            current_step_index=1,
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "macro_analyst", "purpose": "宏观分析"}
            result = await supervisor_node(state, ctx)

        assert result["current_agent_names"] == ["macro_analyst"]
        assert "current_step_index" not in result  # 已对齐，不写

    @pytest.mark.asyncio
    async def test_extra_agent_relocates_to_first_pending(self):
        """计划外 agent 调度 → 槽位指向第一个未完成步骤（归属由 subagent 侧兜底）"""
        from agents.group.nodes.supervisor import supervisor_node

        plan = _accident_plan()
        plan[0]["status"] = "success"
        state = _make_state(plan_steps=plan, current_step_index=2)  # 越界漂移
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "technical_analyst", "purpose": "技术面补充"}
            result = await supervisor_node(state, ctx)

        assert result["current_agent_names"] == ["technical_analyst"]
        assert result["current_step_index"] == 1  # 第一个未完成步骤

    @pytest.mark.asyncio
    async def test_partial_step_reusable_for_rebind(self):
        """partial 步骤允许复用（status != "success"），支持补采重跑"""
        from agents.group.nodes.supervisor import supervisor_node

        plan = [
            _step(1, ["data_collector"], "采集", status="partial"),
            _step(2, ["macro_analyst"], "分析", status="partial"),
        ]
        state = _make_state(plan_steps=plan, current_step_index=2)
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "macro_analyst", "purpose": "重跑分析"}
            result = await supervisor_node(state, ctx)

        assert result["current_step_index"] == 1

    @pytest.mark.asyncio
    async def test_parallel_names_relocate(self):
        """names 并行调度 → 按交集匹配槽位"""
        from agents.group.nodes.supervisor import supervisor_node

        state = _make_state(
            plan_steps=_accident_plan(),
            current_step_index=0,  # 漂移到步骤 1 之前
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {
                "names": ["macro_analyst", "chain_analyst", "risk_analyst", "technical_analyst"],
                "purpose": "并行分析",
            }
            result = await supervisor_node(state, ctx)

        # 交集匹配到步骤 2（index 1），technical_analyst 计划外放行
        assert result["current_agent_names"] == [
            "macro_analyst", "chain_analyst", "risk_analyst", "technical_analyst",
        ]
        assert result["current_step_index"] == 1


# ── D: 计划外一次性放行 ──────────────────────────────────────

class TestExtraDispatchControl:
    """D: supervisor_node 计划外 agent 管控"""

    @pytest.mark.asyncio
    async def test_first_extra_dispatch_allowed(self):
        """首次计划外调度 → 放行并记录 _extra_dispatched"""
        from agents.group.nodes.supervisor import supervisor_node

        plan = [_step(1, ["data_collector"], "采集", status="success")]
        state = _make_state(plan_steps=plan, current_step_index=1)
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "technical_analyst", "purpose": "补充技术面"}
            result = await supervisor_node(state, ctx)

        assert result["current_agent_names"] == ["technical_analyst"]
        assert result["_extra_dispatched"] == ["technical_analyst"]

    @pytest.mark.asyncio
    async def test_executed_extra_agent_skipped_then_early_stop(self):
        """已执行过的计划外 agent 被剔除；全剔除 → early_stop（护栏出口）"""
        from agents.group.nodes.supervisor import supervisor_node

        plan = [_step(1, ["data_collector"], "采集", status="success")]
        state = _make_state(
            plan_steps=plan,
            current_step_index=1,
            _extra_executed=["technical_analyst"],
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "technical_analyst", "purpose": "再来一次"}
            result = await supervisor_node(state, ctx)

        assert result["observation"] == "early_stop"
        assert "current_agent_names" not in result

    @pytest.mark.asyncio
    async def test_mixed_dispatch_filters_only_executed_extra(self):
        """混合调度：计划内 agent 保留，已执行的计划外 agent 剔除"""
        from agents.group.nodes.supervisor import supervisor_node

        state = _make_state(
            plan_steps=_accident_plan(),
            current_step_index=1,
            _extra_executed=["technical_analyst"],
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {
                "names": ["macro_analyst", "technical_analyst"],
                "purpose": "并行分析",
            }
            result = await supervisor_node(state, ctx)

        assert result["current_agent_names"] == ["macro_analyst"]
        # macro_analyst 与 current_idx=1 天然对齐 → 不写 current_step_index
        assert "current_step_index" not in result


# ── B: subagent 槽位归属校验 ─────────────────────────────────

def _make_agent(name="test_agent", agent_type="analysis"):
    from agents.group.subagents.base import SpecializedAgent

    agent = SpecializedAgent()
    agent.agent_name = name
    agent.display_name = name
    agent.description = "测试"
    agent.assigned_skills = []
    agent.agent_type = agent_type
    agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    return agent


def _base_state(**overrides):
    return _make_state(**overrides)


class TestSubagentSlotValidation:
    """B: SpecializedAgent.__call__ 槽位归属校验"""

    @pytest.mark.asyncio
    async def test_slot_relocation_to_owned_step(self):
        """current_idx 指向别人的步骤 → 全表重定位到自己的未完成步骤"""
        agent = _make_agent("macro_analyst")
        state = _base_state(
            plan_steps=[
                _step(1, ["data_collector"], "采集", status="partial"),
                _step(2, ["macro_analyst", "chain_analyst"], "分析"),
            ],
            current_step_index=0,  # 指向 data_collector 的步骤
            current_task_purpose="分析",
        )

        with patch("agents.group.subagents.base.run_react_subgraph"), \
             patch("agents.group.subagents.base.AgentRateLimiter") as limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as log_cls:
            limiter = MagicMock()
            limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "宏观分析完成，足够长的总结内容。" * 5,
                "tool_results": [],
            })
            limiter_cls.return_value = limiter
            log_cls.from_state.return_value = MagicMock()

            result = await agent(state)

        # 写的是步骤 2（index 1），index 推进到 2
        assert result["plan_steps"][0]["status"] == "partial"  # 未被污染
        assert result["plan_steps"][1]["status"] == "success"
        assert result["current_step_index"] == 2

    @pytest.mark.asyncio
    async def test_extra_dispatch_does_not_write_plan(self):
        """全表无匹配 → 计划外执行：不写 plan、不推 index，标记 _extra_executed"""
        agent = _make_agent("technical_analyst")
        state = _base_state(
            plan_steps=[
                _step(1, ["data_collector"], "采集", status="partial"),
                _step(2, ["macro_analyst"], "分析"),
            ],
            current_step_index=1,
            current_task_purpose="技术面补充",
        )

        with patch("agents.group.subagents.base.run_react_subgraph"), \
             patch("agents.group.subagents.base.AgentRateLimiter") as limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as log_cls:
            limiter = MagicMock()
            limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "技术面补充分析完成。" * 10,
                "tool_results": [],
            })
            limiter_cls.return_value = limiter
            log_cls.from_state.return_value = MagicMock()

            result = await agent(state)

        assert "plan_steps" not in result
        assert "current_step_index" not in result
        assert result["_extra_executed"] == ["technical_analyst"]
        assert "technical_analyst" in result["accumulated_data"]  # 数据照常累积
        assert len(result["dispatch_history"]) == 1  # supervisor 仍能看到结果

    @pytest.mark.asyncio
    async def test_explicit_extra_dispatch_flag_short_circuits(self):
        """parallel_executor 显式 _extra_dispatch=True → 直接走计划外分支"""
        agent = _make_agent("chain_analyst")
        state = _base_state(
            plan_steps=[_step(1, ["chain_analyst"], "分析")],
            current_step_index=0,
            current_task_purpose="传导链分析",
        )
        state["_extra_dispatch"] = True

        with patch("agents.group.subagents.base.run_react_subgraph"), \
             patch("agents.group.subagents.base.AgentRateLimiter") as limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as log_cls:
            limiter = MagicMock()
            limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "传导链分析完成。" * 10,
                "tool_results": [],
            })
            limiter_cls.return_value = limiter
            log_cls.from_state.return_value = MagicMock()

            result = await agent(state)

        assert "plan_steps" not in result
        assert "current_step_index" not in result
        assert result["_extra_executed"] == ["chain_analyst"]

    @pytest.mark.asyncio
    async def test_collector_rerun_updates_own_step(self):
        """事故场景正向回归：data_collector 复用 partial 步骤重跑，不污染分析师槽位"""
        agent = _make_agent("data_collector", agent_type="data_collection")
        state = _base_state(
            plan_steps=_accident_plan(),
            current_step_index=0,
            current_task_purpose="补采",
        )

        with patch("agents.group.subagents.base.run_react_subgraph"), \
             patch("agents.group.subagents.base.AgentRateLimiter") as limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as log_cls:
            limiter = MagicMock()
            limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "补采完成，补充了缺失数据。" * 5,
                "tool_results": [],
            })
            limiter_cls.return_value = limiter
            log_cls.from_state.return_value = MagicMock()

            result = await agent(state)

        assert result["plan_steps"][0]["status"] == "success"  # 自己的步骤被更新
        assert result["plan_steps"][1]["status"] == "pending"  # 分析师槽位未被动
        assert result["plan_steps"][1]["agent_names"] == ["macro_analyst", "chain_analyst", "risk_analyst"]
        assert result["current_step_index"] == 1
        assert result["data_collection_doc"]  # 数据底版照常产出


# ── C: parallel_executor 匹配放宽 + 空转护栏 ─────────────────

class _RecordingAgent:
    """记录收到的 state 关键字段，返回可配置的计划内/计划外结果"""

    def __init__(self, name, seen, *, status="success", extra=False):
        self.name = name
        self.seen = seen
        self.status = status
        self.extra = extra

    def configure(self, llm, skill_registry, budget, logger, tool_rate_limiter=None):
        """记录 PE._prepare 的 configure 调用（2026-10-01 16:01 事故回归点）"""
        self.seen[f"{self.name}::configure"] = {
            "llm": llm,
            "logger": logger,
            "budget": budget,
        }
        return self

    async def __call__(self, state, config):
        self.seen[self.name] = {
            "current_step_index": state.get("current_step_index"),
            "task_purpose": state.get("current_task_purpose"),
            "extra_dispatch": bool(state.get("_extra_dispatch")),
        }
        result = copy.deepcopy(state)
        if self.extra:
            result.pop("plan_steps", None)
            result.pop("current_step_index", None)
            result["_extra_executed"] = [self.name]
            result["accumulated_data"] += f"\n\n{self.name} extra result"
        else:
            idx = state["current_step_index"]
            result["plan_steps"][idx]["status"] = self.status
            result["plan_steps"][idx]["result"] = f"{self.name} result"
            result["accumulated_data"] += f"\n\n{self.name} accumulated"
        result["tool_calls"] = list(state.get("tool_calls", []))
        result["dispatch_history"] = list(state.get("dispatch_history", []))
        return result


def _pe_context():
    return SimpleNamespace(
        logger=_Logger(),
        budget=object(),
        skill_registry=object(),
        progress_reporter=None,
    )


class TestParallelExecutorGuard:
    """C: 匹配放宽、槽位回溯、计划外放行、空转护栏"""

    @pytest.mark.asyncio
    async def test_partial_step_still_bindable(self):
        """匹配放宽：partial 步骤可重新绑定（补采重跑），success 不行"""
        import agents.group.dispatcher as dispatcher

        seen = {}
        agents = {"data_collector": _RecordingAgent("data_collector", seen)}
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            state = _make_state(
                plan_steps=[
                    _step(1, ["data_collector"], "采集", status="partial"),
                    _step(2, ["macro_analyst"], "分析", status="success"),
                ],
                current_step_index=2,  # 已越界漂移（事故现场）
                current_agent_names=["data_collector"],
            )
            result = await parallel_executor_node(state, _pe_context())
        finally:
            monkeypatch.undo()

        assert seen["data_collector"]["current_step_index"] == 0  # 回溯绑定到步骤 1
        assert result["plan_steps"][0]["status"] == "success"
        assert result["_empty_dispatch_count"] == 0

    @pytest.mark.asyncio
    async def test_empty_dispatch_counts_then_guard_trips(self):
        """连续 3 轮 0 步骤绑定 → 第 3 轮 observation=early_stop"""
        import agents.group.dispatcher as dispatcher

        plan = _accident_plan()
        plan[1]["status"] = "success"  # 步骤 2 已完成（事故现场：槽位被污染覆盖后完成）

        for round_no in (1, 2, 3):
            seen = {}
            agents = {"macro_analyst": _RecordingAgent("macro_analyst", seen)}
            monkeypatch = pytest.MonkeyPatch()
            try:
                monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
                state = _make_state(
                    plan_steps=copy.deepcopy(plan),
                    current_step_index=2,
                    current_agent_names=["macro_analyst"],
                    _empty_dispatch_count=round_no - 1,
                )
                result = await parallel_executor_node(state, _pe_context())
            finally:
                monkeypatch.undo()

            assert result["_empty_dispatch_count"] == round_no
            if round_no < 3:
                assert "observation" not in result  # 前两轮回 supervisor
            else:
                assert result["observation"] == "early_stop"  # 护栏触发

    @pytest.mark.asyncio
    async def test_bound_step_resets_empty_count(self):
        """本轮有步骤绑定 → 空转计数清零"""
        import agents.group.dispatcher as dispatcher

        seen = {}
        agents = {"macro_analyst": _RecordingAgent("macro_analyst", seen)}
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            state = _make_state(
                plan_steps=_accident_plan(),
                current_step_index=1,
                current_agent_names=["macro_analyst"],
                _empty_dispatch_count=2,  # 前两轮空转过
            )
            result = await parallel_executor_node(state, _pe_context())
        finally:
            monkeypatch.undo()

        assert result["_empty_dispatch_count"] == 0
        assert "observation" not in result

    @pytest.mark.asyncio
    async def test_extra_agent_dispatched_without_binding(self):
        """计划外 agent：放行执行（显式 _extra_dispatch=True），结果不绑步骤并合并 _extra_executed"""
        import agents.group.dispatcher as dispatcher

        seen = {}
        agents = {"technical_analyst": _RecordingAgent("technical_analyst", seen, extra=True)}
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            state = _make_state(
                plan_steps=_accident_plan(),
                current_step_index=1,
                current_agent_names=["technical_analyst"],
            )
            result = await parallel_executor_node(state, _pe_context())
        finally:
            monkeypatch.undo()

        assert seen["technical_analyst"]["extra_dispatch"] is True
        assert result["_extra_executed"] == ["technical_analyst"]
        assert "technical_analyst extra result" in result["accumulated_data"]
        assert result["plan_steps"][1]["status"] == "pending"  # 未占用计划槽位
        assert result["_empty_dispatch_count"] == 1  # 0 步骤绑定 → 计数

    @pytest.mark.asyncio
    async def test_already_executed_extra_agent_skipped(self):
        """已执行过的计划外 agent → 跳过不执行，计入空转"""
        import agents.group.dispatcher as dispatcher

        seen = {}
        agents = {"technical_analyst": _RecordingAgent("technical_analyst", seen, extra=True)}
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            state = _make_state(
                plan_steps=_accident_plan(),
                current_step_index=2,
                current_agent_names=["technical_analyst"],
                _extra_executed=["technical_analyst"],
                _empty_dispatch_count=1,
            )
            result = await parallel_executor_node(state, _pe_context())
        finally:
            monkeypatch.undo()

        assert seen == {}  # 未被执行
        assert result["_empty_dispatch_count"] == 2


class TestParallelExecutorExceptionGuard:
    """C 补充（2026-10-01 16:01 事故）：绑定 agent 全部执行异常的护栏"""

    @pytest.mark.asyncio
    async def test_bound_agents_all_exception_counts_as_idle_and_marks_failed(self):
        """绑定成功但全部抛异常 → 步骤标 failed 可见 + 空转计数 +1"""
        import agents.group.dispatcher as dispatcher

        class _BoomAgent:
            def __init__(self, name):
                self.name = name

            def configure(self, llm, skill_registry, budget, logger, tool_rate_limiter=None):
                return self

            async def __call__(self, state, config):
                raise RuntimeError("'NoneType' object has no attribute 'phase'")

        agents = {
            n: _BoomAgent(n)
            for n in ("macro_analyst", "chain_analyst", "risk_analyst")
        }
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            state = _make_state(
                plan_steps=_accident_plan(),
                current_step_index=1,
                current_agent_names=["macro_analyst", "chain_analyst", "risk_analyst"],
            )
            result = await parallel_executor_node(state, _pe_context())
        finally:
            monkeypatch.undo()

        # 异常写回绑定步骤，supervisor 下一轮看得到失败原因（不再盲目重派 pending 步骤）
        assert result["plan_steps"][1]["status"] == "failed"
        assert "执行异常" in result["plan_steps"][1]["feedback"]
        # 3 个绑定 agent 全部异常 → 步骤 retry_count 逐个 +1（3 次）
        assert result["plan_steps"][1]["retry_count"] == 3
        # 全异常 = 无有效计划进展 → 空转计数
        assert result["_empty_dispatch_count"] == 1
        assert "observation" not in result

    @pytest.mark.asyncio
    async def test_consecutive_exception_rounds_trip_guard(self):
        """连续 3 轮全异常 → 第 3 轮 observation=early_stop"""
        import agents.group.dispatcher as dispatcher

        class _BoomAgent:
            def configure(self, llm, skill_registry, budget, logger, tool_rate_limiter=None):
                return self

            async def __call__(self, state, config):
                raise RuntimeError("boom")

        agents = {"macro_analyst": _BoomAgent()}
        plan = [
            _step(1, ["data_collector"], "采集", status="success"),
            _step(2, ["macro_analyst"], "分析", status="pending"),
        ]
        for round_no in (1, 2, 3):
            monkeypatch = pytest.MonkeyPatch()
            try:
                monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
                state = _make_state(
                    plan_steps=copy.deepcopy(plan),
                    current_step_index=1,
                    current_agent_names=["macro_analyst"],
                    _empty_dispatch_count=round_no - 1,
                )
                result = await parallel_executor_node(state, _pe_context())
            finally:
                monkeypatch.undo()

            assert result["_empty_dispatch_count"] == round_no
            if round_no < 3:
                assert "observation" not in result
            else:
                assert result["observation"] == "early_stop"

    @pytest.mark.asyncio
    async def test_partial_exception_still_has_progress(self):
        """部分异常部分成功（步骤有写回）→ 计数清零，不误伤"""
        import agents.group.dispatcher as dispatcher

        class _BoomAgent:
            def configure(self, llm, skill_registry, budget, logger, tool_rate_limiter=None):
                return self

            async def __call__(self, state, config):
                raise RuntimeError("boom")

        seen = {}
        agents = {
            "macro_analyst": _RecordingAgent("macro_analyst", seen),
            "chain_analyst": _BoomAgent(),
        }
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            state = _make_state(
                plan_steps=[
                    _step(1, ["data_collector"], "采集", status="success"),
                    _step(2, ["macro_analyst", "chain_analyst"], "分析"),
                ],
                current_step_index=1,
                current_agent_names=["macro_analyst", "chain_analyst"],
                _empty_dispatch_count=2,  # 前两轮已空转
            )
            result = await parallel_executor_node(state, _pe_context())
        finally:
            monkeypatch.undo()

        assert result["plan_steps"][1]["status"] == "success"  # macro_analyst 写回
        assert result["_empty_dispatch_count"] == 0  # 有进展 → 清零
        assert "observation" not in result

    @pytest.mark.asyncio
    async def test_prepare_unconditionally_configures_new_instance(self):
        """回归核心：ctx.budget 无 _llm 属性（BudgetController 真实形态）时，
        _prepare 仍必须无条件 configure 新实例（logger/llm 注入非 None）"""
        import agents.group.dispatcher as dispatcher

        seen = {}
        agents = {"macro_analyst": _RecordingAgent("macro_analyst", seen)}
        sentinel_llm = object()
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(dispatcher, "get_agent_instance", agents.__getitem__)
            monkeypatch.setattr("utils.llm_factory.get_llm", lambda: sentinel_llm)
            ctx = _pe_context()  # budget=object()，无 _llm 属性
            state = _make_state(
                plan_steps=_accident_plan(),
                current_step_index=1,
                current_agent_names=["macro_analyst"],
            )
            result = await parallel_executor_node(state, ctx)
        finally:
            monkeypatch.undo()

        cfg = seen["macro_analyst::configure"]
        assert cfg["llm"] is sentinel_llm  # LLM 与图构建同源
        assert cfg["logger"] is ctx.logger  # logger 注入（事故点：为 None 则 phase 崩）
        assert cfg["budget"] is ctx.budget
        assert result["plan_steps"][1]["status"] == "success"


# ── 路由 ─────────────────────────────────────────────────────

def test_route_parallel_early_stop_to_report():
    """空转护栏触发 → early_stop 路由到 report（拿已有数据出报告）"""
    state = _make_state(observation="early_stop")
    assert _route_after_parallel(state) == "report"


def test_route_parallel_normal_back_to_supervisor():
    state = _make_state()
    assert _route_after_parallel(state) == "supervisor"


def test_route_parallel_error_to_exception_summary():
    state = _make_state(observation="error")
    assert _route_after_parallel(state) == "exception_summary"

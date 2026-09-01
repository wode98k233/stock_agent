"""
测试目标: agents/group/nodes/supervisor.py
覆盖范围:
  - _generate_plan: 计划生成、空计划、LLM 异常、agent_name 映射、run_group 分配
  - supervisor_node: 首次进入、后续调度、finish 路由、未知 agent、LLM 异常
  - _evaluate_step: success/failed/need_info/unknown 状态
  - _assign_run_groups: 并发分组
  - _build_dispatch_messages: 消息结构
Mock 策略: mock llm_json_with_retry、GroupMessageLogger、AgentContext
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from agents.group.state import AgentStep


# ── helpers ──────────────────────────────────────────────────

def _make_step(step=1, agent_names=None, task_purpose="test", status="pending",
               result="", feedback="", retry_count=0, run_group=0):
    return AgentStep(
        step=step,
        agent_names=agent_names or ["technical_analyst"],
        task_purpose=task_purpose,
        input_params={},
        status=status,
        result=result,
        feedback=feedback,
        requests=[],
        retry_count=retry_count,
        executed_at="",
        run_group=run_group,
        depends_on=[],
    )


def _make_state(**overrides):
    base = {
        "input": "分析贵州茅台",
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
    }
    base.update(overrides)
    return base


def _make_ctx():
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.logger.phase = MagicMock()
    ctx.logger.info = MagicMock()
    ctx.logger.warning = MagicMock()
    ctx.logger.error = MagicMock()
    ctx.budget = MagicMock()
    ctx.budget._llm = MagicMock()
    ctx.progress_reporter = MagicMock()
    ctx.memory = MagicMock()
    ctx.memory.enabled = False
    ctx.memory.get_history = MagicMock(return_value=[])
    return ctx


# ── _evaluate_step ───────────────────────────────────────────

class TestEvaluateStep:
    """_evaluate_step: 步骤结果评估"""

    def test_success_returns_next(self):
        from agents.group.nodes.supervisor import _evaluate_step
        step = _make_step(status="success", result="分析完成")
        action, reasoning = _evaluate_step(step, "")
        assert action == "next"
        assert "完成" in reasoning

    def test_need_info_returns_call_agent(self):
        from agents.group.nodes.supervisor import _evaluate_step
        step = _make_step(status="need_info", feedback="需要更多数据")
        action, reasoning = _evaluate_step(step, "")
        assert action == "call_agent"
        assert "补充" in reasoning

    def test_failed_below_max_retries_returns_retry(self):
        from agents.group.nodes.supervisor import _evaluate_step
        with patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_cfg.GROUP_AGENT_MAX_RETRIES = 2
            step = _make_step(status="failed", retry_count=0)
            action, reasoning = _evaluate_step(step, "")
        assert action == "retry"
        assert "重试" in reasoning

    def test_failed_at_max_retries_returns_finish(self):
        from agents.group.nodes.supervisor import _evaluate_step
        with patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_cfg.GROUP_AGENT_MAX_RETRIES = 2
            step = _make_step(status="failed", retry_count=2)
            action, reasoning = _evaluate_step(step, "")
        assert action == "finish"
        assert "重试次数用尽" in reasoning

    def test_unknown_status_returns_next(self):
        from agents.group.nodes.supervisor import _evaluate_step
        step = _make_step(status="unknown_status")
        action, _ = _evaluate_step(step, "")
        assert action == "next"


# ── _assign_run_groups ───────────────────────────────────────

class TestAssignRunGroups:
    """_assign_run_groups: 并发分组分配"""

    def test_no_deps_groups_by_concurrency(self):
        from agents.group.nodes.supervisor import _assign_run_groups
        steps = [_make_step(step=i+1) for i in range(4)]
        result = _assign_run_groups(steps, max_concurrent=2)
        groups = [s["run_group"] for s in result]
        assert groups == [1, 1, 2, 2]

    def test_with_deps_sequential_groups(self):
        from agents.group.nodes.supervisor import _assign_run_groups
        steps = [_make_step(step=i+1) for i in range(3)]
        steps[1]["depends_on"] = [1]
        result = _assign_run_groups(steps, max_concurrent=2)
        groups = [s["run_group"] for s in result]
        assert groups == [1, 2, 3]

    def test_single_step(self):
        from agents.group.nodes.supervisor import _assign_run_groups
        steps = [_make_step(step=1)]
        result = _assign_run_groups(steps, max_concurrent=4)
        assert result[0]["run_group"] == 1

    def test_empty_steps(self):
        from agents.group.nodes.supervisor import _assign_run_groups
        result = _assign_run_groups([], max_concurrent=2)
        assert result == []


# ── _build_dispatch_messages ─────────────────────────────────

class TestBuildDispatchMessages:
    """_build_dispatch_messages: 调度消息结构"""

    def test_basic_structure(self):
        from agents.group.nodes.supervisor import _build_dispatch_messages
        state = _make_state()
        msgs = _build_dispatch_messages(state)
        # system: 角色 + 用户问题 + 调度指令 + user: 触发
        assert len(msgs) >= 3
        assert msgs[0][0] == "system"  # 角色
        assert msgs[-1][0] == "user"   # 触发

    def test_with_plan(self):
        from agents.group.nodes.supervisor import _build_dispatch_messages
        state = _make_state(original_plan=[
            {"agent_names": ["technical_analyst"], "task_purpose": "技术分析"},
        ])
        msgs = _build_dispatch_messages(state)
        plan_msg = [m for m in msgs if "执行计划" in m[1]]
        # supervisor prompt 中也包含"执行计划"，因此匹配 2 条（prompt + plan）
        assert len(plan_msg) == 2

    def test_with_dispatch_history(self):
        from agents.group.nodes.supervisor import _build_dispatch_messages
        state = _make_state(dispatch_history=[
            {"dispatch": {"name": "technical_analyst", "purpose": "技术分析"}, "result": "分析完成"},
        ])
        msgs = _build_dispatch_messages(state)
        # 历史消息应该被加入
        ai_msgs = [m for m in msgs if m[0] == "ai"]
        assert len(ai_msgs) >= 1


# ── supervisor_node ──────────────────────────────────────────

class TestSupervisorNode:
    """supervisor_node: Supervisor 调度决策"""

    @pytest.mark.asyncio
    async def test_first_entry_generates_plan(self):
        """首次进入（无 plan）→ 调用 _generate_plan"""
        from agents.group.nodes.supervisor import supervisor_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor._generate_plan") as mock_gen:
            mock_gen.return_value = {"plan_steps": [_make_step()], "current_agent_names": ["technical_analyst"]}
            result = await supervisor_node(state, ctx)
            mock_gen.assert_called_once()
            assert "plan_steps" in result

    @pytest.mark.asyncio
    async def test_finish_dispatch_returns_early_stop(self):
        """LLM 返回 finish → observation=early_stop"""
        from agents.group.nodes.supervisor import supervisor_node
        step = _make_step(step=1, status="success", result="完成")
        state = _make_state(
            plan_steps=[step],
            current_step_index=1,
            current_agent_names=[],
            current_task_purpose="",
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "finish", "purpose": ""}
            result = await supervisor_node(state, ctx)
        assert result.get("observation") == "early_stop"

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_error(self):
        """LLM 返回未知 agent → observation=error"""
        from agents.group.nodes.supervisor import supervisor_node
        step = _make_step(step=1, status="success", result="完成")
        state = _make_state(
            plan_steps=[step],
            current_step_index=1,
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "nonexistent_agent", "purpose": "test"}
            result = await supervisor_node(state, ctx)
        assert result.get("observation") == "error"
        assert "未知" in result.get("_error_message", "")

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error(self):
        """LLM 调用异常 → observation=error"""
        from agents.group.nodes.supervisor import supervisor_node
        step = _make_step(step=1, status="success", result="完成")
        state = _make_state(plan_steps=[step], current_step_index=1)
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.side_effect = RuntimeError("LLM 超时")
            result = await supervisor_node(state, ctx)
        assert result.get("observation") == "error"
        assert "LLM" in result.get("_error_message", "")

    @pytest.mark.asyncio
    async def test_valid_dispatch_sets_agent_names(self):
        """LLM 返回有效 agent → 设置 current_agent_names"""
        from agents.group.nodes.supervisor import supervisor_node
        step = _make_step(step=1, status="success", result="完成")
        state = _make_state(plan_steps=[step], current_step_index=1)
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"name": "macro_analyst", "purpose": "宏观分析"}
            result = await supervisor_node(state, ctx)
        assert result["current_agent_names"] == ["macro_analyst"]
        assert result["current_task_purpose"] == "宏观分析"

    @pytest.mark.asyncio
    async def test_failed_step_triggers_retry(self):
        """上一步 failed + 未超限 → 重试"""
        from agents.group.nodes.supervisor import supervisor_node
        step = _make_step(step=1, status="failed", retry_count=0)
        state = _make_state(plan_steps=[step], current_step_index=1)
        ctx = _make_ctx()

        with patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_cfg.GROUP_AGENT_MAX_RETRIES = 2
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2
            result = await supervisor_node(state, ctx)
        assert result["current_step_index"] == 0  # 回到当前步骤
        assert result["plan_steps"][0]["retry_count"] == 1


# ── _generate_plan ───────────────────────────────────────────

class TestGeneratePlan:
    """_generate_plan: 初始计划生成"""

    @pytest.mark.asyncio
    async def test_valid_plan(self):
        """LLM 返回合法计划 → 生成 steps + 设置 first agent"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        msg_log = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_llm.return_value = {"steps": [
                {"agent_names": ["technical_analyst"], "task_purpose": "技术分析"},
                {"agent_names": ["macro_analyst"], "task_purpose": "宏观分析"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2
            result = await _generate_plan(state, ctx, MagicMock(), msg_log)

        assert len(result["plan_steps"]) == 2
        assert result["current_agent_names"] == ["technical_analyst"]
        assert result["current_step_index"] == 0

    @pytest.mark.asyncio
    async def test_empty_plan_returns_error(self):
        """LLM 返回空计划 → observation=error"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        msg_log = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.return_value = {"steps": []}
            result = await _generate_plan(state, ctx, MagicMock(), msg_log)

        assert result["plan_steps"] == []
        assert result["observation"] == "error"

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error(self):
        """LLM 调用异常 → observation=error"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        msg_log = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm:
            mock_llm.side_effect = RuntimeError("timeout")
            result = await _generate_plan(state, ctx, MagicMock(), msg_log)

        assert result["observation"] == "error"

    @pytest.mark.asyncio
    async def test_display_name_mapping(self):
        """LLM 返回中文名 → 自动映射为英文 agent_name"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        msg_log = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg, \
             patch("agents.group.nodes.supervisor._DISPLAY_NAME_MAP", {"K线技术分析师": "technical_analyst"}):
            mock_llm.return_value = {"steps": [
                {"agent_names": ["K线技术分析师"], "task_purpose": "技术分析"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2
            result = await _generate_plan(state, ctx, MagicMock(), msg_log)

        assert result["plan_steps"][0]["agent_names"] == ["technical_analyst"]

    @pytest.mark.asyncio
    async def test_invalid_agent_names_filtered(self):
        """LLM 返回无效 agent_name → 被过滤掉"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        msg_log = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_llm.return_value = {"steps": [
                {"agent_names": ["nonexistent"], "task_purpose": "无效"},
                {"agent_names": ["technical_analyst"], "task_purpose": "有效"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2
            result = await _generate_plan(state, ctx, MagicMock(), msg_log)

        assert len(result["plan_steps"]) == 1
        assert result["plan_steps"][0]["agent_names"] == ["technical_analyst"]

    @pytest.mark.asyncio
    async def test_single_agent_name_fallback(self):
        """LLM 返回 agent_name（单数）→ 兼容为列表"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        msg_log = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_llm, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_llm.return_value = {"steps": [
                {"agent_name": "technical_analyst", "task_purpose": "技术分析"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2
            result = await _generate_plan(state, ctx, MagicMock(), msg_log)

        assert result["plan_steps"][0]["agent_names"] == ["technical_analyst"]

    @pytest.mark.asyncio
    async def test_budget_passed_to_llm(self):
        """验证 budget 参数传递给 llm_json_with_retry"""
        from agents.group.nodes.supervisor import _generate_plan
        state = _make_state()
        ctx = _make_ctx()
        ctx.budget = MagicMock()
        msg_log = MagicMock()
        mock_llm = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_retry, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_retry.return_value = {"steps": [
                {"agent_names": ["technical_analyst"], "task_purpose": "test"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2
            await _generate_plan(state, ctx, mock_llm, msg_log)

        _, kwargs = mock_retry.call_args
        assert kwargs.get("budget") is ctx.budget


# ── Message Order Tests ───────────────────────────────────────

class TestDispatchMessageOrder:
    """_build_dispatch_messages: 固定指令在动态内容之前"""

    def test_static_dispatch_instruction_before_dynamic(self):
        """固定 supervisor prompt 和调度指令在所有动态内容之前。"""
        from agents.group.nodes.supervisor import _build_dispatch_messages

        state = _make_state(
            input="QUESTION_MARKER",
            original_plan=[{"agent_names": ["technical_analyst"], "task_purpose": "PLAN_MARKER"}],
            data_collection_doc="DATA_MARKER",
            dispatch_history=[
                {"dispatch": {"name": "technical_analyst", "purpose": "HISTORY_MARKER"}, "result": "结果", "status": "success"}
            ],
        )

        msgs = _build_dispatch_messages(state)
        contents = [m[1] for m in msgs]

        # 前 2 条为固定内容：supervisor prompt + 调度指令
        assert msgs[0][0] == "system"
        assert "多 Agent 调度器" in msgs[0][1] or "调度器" in msgs[0][1]
        assert msgs[1][0] == "system"
        assert "调度子 agent" in msgs[1][1] or "调度" in msgs[1][1]

        # 动态标记（问题、计划、数据、历史）都在固定指令之后
        static_end_idx = 1  # 前 2 条（索引 0,1）为静态
        question_idx = next(i for i, c in enumerate(contents) if "QUESTION_MARKER" in c)
        plan_idx = next(i for i, c in enumerate(contents) if "PLAN_MARKER" in c)
        data_idx = next(i for i, c in enumerate(contents) if "DATA_MARKER" in c)
        history_idx = next(i for i, c in enumerate(contents) if "HISTORY_MARKER" in c)

        assert question_idx > static_end_idx
        assert plan_idx > static_end_idx
        assert data_idx > static_end_idx
        assert history_idx > static_end_idx

        # 最后一条为 user 触发
        assert msgs[-1][0] == "user"


class TestGeneratePlanMessageOrder:
    """_generate_plan: 固定 prompt + 指令在前，history 在中，动态内容在后"""

    @pytest.mark.asyncio
    async def test_static_first_history_middle_dynamic_last(self):
        """固定 supervisor prompt 和规划指令在前，history 在中，动态问题在后。"""
        from agents.group.nodes.supervisor import _generate_plan

        state = _make_state(
            input="QUESTION_MARKER",
            constraints="CONSTRAINT_MARKER",
        )
        ctx = _make_ctx()
        ctx.memory.enabled = True
        ctx.memory.get_history.return_value = [("human", "HISTORY_MARKER")]
        msg_log = MagicMock()
        mock_llm = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_retry, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_retry.return_value = {"steps": [
                {"agent_names": ["technical_analyst"], "task_purpose": "test"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2

            # 捕获发送给 LLM 的 messages
            captured = {}

            def fake_retry(llm, messages, **kwargs):
                captured["messages"] = messages
                return {"steps": [{"agent_names": ["technical_analyst"], "task_purpose": "test"}]}

            mock_retry.side_effect = fake_retry
            await _generate_plan(state, ctx, mock_llm, msg_log)

        msgs = captured["messages"]
        contents = [m[1] if isinstance(m, tuple) else m.content for m in msgs]

        # 前 2 条为固定内容
        assert "多 Agent 调度器" in contents[0] or "调度器" in contents[0]
        assert "生成执行计划" in contents[1]

        # history 在固定内容之后、动态内容之前
        history_idx = next(i for i, c in enumerate(contents) if "HISTORY_MARKER" in c)
        assert history_idx > 1

        # 动态问题在 history 之后
        question_idx = next(i for i, c in enumerate(contents) if "QUESTION_MARKER" in c)
        assert question_idx > history_idx

        # 约束在问题之后
        constraint_idx = next(i for i, c in enumerate(contents) if "CONSTRAINT_MARKER" in c)
        assert constraint_idx > question_idx

    @pytest.mark.asyncio
    async def test_no_empty_dynamic_messages(self):
        """无约束、无 data contract 时不产生空消息。"""
        from agents.group.nodes.supervisor import _generate_plan

        state = _make_state(input="分析股票")
        ctx = _make_ctx()
        msg_log = MagicMock()
        mock_llm = MagicMock()

        with patch("agents.group.nodes.supervisor.llm_json_with_retry") as mock_retry, \
             patch("agents.group.nodes.supervisor.Config") as mock_cfg:
            mock_retry.return_value = {"steps": [
                {"agent_names": ["technical_analyst"], "task_purpose": "test"},
            ]}
            mock_cfg.GROUP_MAX_CONCURRENT_AGENTS = 2

            captured = {}

            def fake_retry(llm, messages, **kwargs):
                captured["messages"] = messages
                return {"steps": [{"agent_names": ["technical_analyst"], "task_purpose": "test"}]}

            mock_retry.side_effect = fake_retry
            await _generate_plan(state, ctx, mock_llm, msg_log)

        msgs = captured["messages"]
        # 没有空内容的 system 消息
        empty_systems = [m for m in msgs if isinstance(m, tuple) and m[0] == "system" and m[1] == ""]
        assert len(empty_systems) == 0

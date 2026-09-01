"""
测试目标: agents/group/nodes/report_enhance.py
覆盖范围:
  - report_enhance_node: 正常报告生成、空结果降级、报告后处理调用、dashboard 追加
  - 结果合并逻辑（accumulated_data 优先 vs plan_steps fallback）
  - 异常降级（报告后处理失败、dashboard 失败不影响主流程）
Mock 策略: mock run_report_post_processing、append_dashboard、GroupMessageLogger
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_state(**overrides):
    base = {
        "input": "分析贵州茅台",
        "plan_steps": [
            {"step": 1, "agent_names": ["technical_analyst"], "task_purpose": "技术分析",
             "input_params": {}, "status": "success", "result": "技术面看涨", "feedback": "",
             "requests": [], "retry_count": 0, "executed_at": "", "run_group": 1, "depends_on": []},
            {"step": 2, "agent_names": ["macro_analyst"], "task_purpose": "宏观分析",
             "input_params": {}, "status": "success", "result": "宏观环境良好", "feedback": "",
             "requests": [], "retry_count": 0, "executed_at": "", "run_group": 2, "depends_on": []},
        ],
        "current_step_index": 2,
        "accumulated_data": "",
        "tool_calls": [
            {"tool_name": "mx_get_kline", "tool_input": "{}", "tool_output": "data", "tool_call_id": ""},
        ],
        "template_id": None,
        "selected_skills": [],
        "dialog_uuid": "test-uuid",
        "task_id": "test-task",
        "constraints": "",
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
    ctx.progress_reporter = MagicMock()
    return ctx


class TestReportEnhanceNode:
    """report_enhance_node: 报告生成节点"""

    @pytest.mark.asyncio
    async def test_uses_accumulated_data_when_available(self):
        """accumulated_data 有值 → 优先使用"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="累积的分析数据")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            result = await report_enhance_node(state, ctx)

        assert "累积的分析数据" in result["response"]

    @pytest.mark.asyncio
    async def test_falls_back_to_plan_results(self):
        """accumulated_data 为空 → 从 plan_steps 合并"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            result = await report_enhance_node(state, ctx)

        assert "技术面看涨" in result["response"]
        assert "宏观环境良好" in result["response"]

    @pytest.mark.asyncio
    async def test_no_results_returns_fallback(self):
        """无任何结果 → 返回降级消息"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(
            accumulated_data="",
            plan_steps=[{"step": 1, "agent_names": ["test"], "task_purpose": "test",
                         "status": "failed", "result": "", "input_params": {},
                         "feedback": "", "requests": [], "retry_count": 0,
                         "executed_at": "", "run_group": 1, "depends_on": []}],
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            result = await report_enhance_node(state, ctx)

        assert "无可用" in result["response"]

    @pytest.mark.asyncio
    async def test_report_post_processing_called(self):
        """REPORT_ENABLE_ANALYSIS_ENGINE=True → 调用报告后处理"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.nodes.report_enhance.run_report_post_processing", new_callable=AsyncMock) as mock_post:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            mock_post.return_value = "增强后的报告"

            result = await report_enhance_node(state, ctx)

        mock_post.assert_called_once()
        assert result["response"] == "增强后的报告"

    @pytest.mark.asyncio
    async def test_report_post_processing_failure_uses_original(self):
        """报告后处理失败 → 使用原始结果"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="原始数据")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.nodes.report_enhance.run_report_post_processing", new_callable=AsyncMock) as mock_post:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            mock_post.side_effect = RuntimeError("后处理失败")

            result = await report_enhance_node(state, ctx)

        assert "原始数据" in result["response"]

    @pytest.mark.asyncio
    async def test_dashboard_failure_does_not_affect_report(self):
        """仪表盘生成失败 → 不影响报告"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="报告内容")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.analysis.dashboard_node.append_dashboard", new_callable=AsyncMock) as mock_dash:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = True
            mock_log_cls.from_state.return_value = MagicMock()
            mock_dash.side_effect = RuntimeError("dashboard 失败")

            result = await report_enhance_node(state, ctx)

        assert "报告内容" in result["response"]

    @pytest.mark.asyncio
    async def test_tool_calls_empty_for_group_mode(self):
        """agent_group 模式：报告后处理时 tool_calls=[]"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.nodes.report_enhance.run_report_post_processing", new_callable=AsyncMock) as mock_post:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            mock_post.return_value = "报告"

            await report_enhance_node(state, ctx)

        _, kwargs = mock_post.call_args
        assert kwargs.get("tool_calls") == []

    @pytest.mark.asyncio
    async def test_progress_final_called(self):
        """调用 progress.final"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            await report_enhance_node(state, ctx)

        ctx.progress_reporter.final.assert_called_once()

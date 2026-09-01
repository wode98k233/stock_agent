"""
测试目标: agents/group/message_logger.py
覆盖范围:
  - GroupMessageLogger.__init__: 构造参数
  - GroupMessageLogger.from_state: 从 state 构建
  - plan/step_start/step_result/observe/adjust/replan/resolve/early_stop/error: 各事件
  - _emit: SSE 推送（正常/异常）
  - _save: DB 写入（正常/异常/无 dialog_uuid）
  - _format_plan: 计划格式化
Mock 策略: mock save_group_message、ProgressReporter
"""
import pytest
from unittest.mock import MagicMock, patch


class TestGroupMessageLoggerInit:
    """构造和 from_state"""

    def test_init_with_all_params(self):
        from agents.group.message_logger import GroupMessageLogger
        progress = MagicMock()
        logger = MagicMock()
        log = GroupMessageLogger(progress=progress, dialog_uuid="d1", task_id="t1", logger=logger)
        assert log._dialog_uuid == "d1"
        assert log._task_id == "t1"
        assert log._progress is progress

    def test_init_defaults(self):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger()
        assert log._dialog_uuid == ""
        assert log._task_id == ""

    def test_from_state(self):
        from agents.group.message_logger import GroupMessageLogger
        state = {"dialog_uuid": "uuid-123", "task_id": "task-456"}
        log = GroupMessageLogger.from_state(state, progress=MagicMock(), logger=MagicMock())
        assert log._dialog_uuid == "uuid-123"
        assert log._task_id == "task-456"

    def test_from_state_missing_keys(self):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger.from_state({})
        assert log._dialog_uuid == ""
        assert log._task_id == ""


class TestFormatPlan:
    """_format_plan: 计划格式化"""

    def test_single_step(self):
        from agents.group.message_logger import GroupMessageLogger
        steps = [{"step": 1, "agent_names": ["technical_analyst"], "run_group": 1, "task_purpose": "技术分析"}]
        result = GroupMessageLogger._format_plan(steps)
        assert "technical_analyst" in result
        assert "技术分析" in result
        assert "组1" in result

    def test_multiple_steps(self):
        from agents.group.message_logger import GroupMessageLogger
        steps = [
            {"step": 1, "agent_names": ["a"], "run_group": 1, "task_purpose": "任务A"},
            {"step": 2, "agent_names": ["b", "c"], "run_group": 2, "task_purpose": "任务B"},
        ]
        result = GroupMessageLogger._format_plan(steps)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "b, c" in lines[1]


class TestEventMethods:
    """各事件方法的调用验证"""

    @patch("agents.group.message_logger.save_group_message")
    def test_plan_emits_and_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        progress = MagicMock()
        log = GroupMessageLogger(progress=progress, dialog_uuid="d1", task_id="t1")
        steps = [{"step": 1, "agent_names": ["a"], "run_group": 1, "task_purpose": "test"}]

        log.plan(steps)

        progress.report.assert_called_once()
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["dialog_uuid"] == "d1"
        assert call_kwargs["role"] == "dispatcher"
        assert call_kwargs["msg_type"] == "plan"

    @patch("agents.group.message_logger.save_group_message")
    def test_step_start_emits_and_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        progress = MagicMock()
        log = GroupMessageLogger(progress=progress, dialog_uuid="d1")

        log.step_start(1, ["technical_analyst"], "技术分析")

        progress.report.assert_called_once()
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["role"] == "dispatcher"
        assert call_kwargs["msg_type"] == "task"
        assert call_kwargs["agent_name"] == "technical_analyst"

    @patch("agents.group.message_logger.save_group_message")
    def test_step_result_saves_with_extra(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.step_result(1, ["macro_analyst"], "success", "分析完成")

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["role"] == "sub_agent"
        assert call_kwargs["msg_type"] == "result"
        assert call_kwargs["extra"]["status"] == "success"

    @patch("agents.group.message_logger.save_group_message")
    def test_observe_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.observe(1, "next", "结果充分")

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["msg_type"] == "observe"

    @patch("agents.group.message_logger.save_group_message")
    def test_adjust_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.adjust([{"task_purpose": "补充分析"}])

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["msg_type"] == "adjust_plan"

    @patch("agents.group.message_logger.save_group_message")
    def test_replan_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.replan([{"step": 1, "agent_names": ["a"], "run_group": 1, "task_purpose": "新计划"}])

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["msg_type"] == "replan_plan"

    @patch("agents.group.message_logger.save_group_message")
    def test_resolve_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.resolve([{"type": "data"}], 1)

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["msg_type"] == "adjust_notice"

    @patch("agents.group.message_logger.save_group_message")
    def test_early_stop_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.early_stop("所有分析完成")

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["msg_type"] == "early_stop"

    @patch("agents.group.message_logger.save_group_message")
    def test_error_saves(self, mock_save):
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="d1")

        log.error("发生错误")

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["msg_type"] == "error"


class TestEdgeCases:
    """边界和异常场景"""

    @patch("agents.group.message_logger.save_group_message")
    def test_no_dialog_uuid_skips_save(self, mock_save):
        """无 dialog_uuid → 不写 DB"""
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(dialog_uuid="")

        log.error("错误")

        mock_save.assert_not_called()

    def test_emit_handles_progress_exception(self):
        """progress.report 异常不影响主流程"""
        from agents.group.message_logger import GroupMessageLogger
        progress = MagicMock()
        progress.report.side_effect = RuntimeError("SSE 断开")
        log = GroupMessageLogger(progress=progress, dialog_uuid="d1")

        # 不应抛出异常
        with patch("agents.group.message_logger.save_group_message"):
            log.observe(1, "test", "reason")

    @patch("agents.group.message_logger.save_group_message")
    def test_save_handles_db_exception(self, mock_save):
        """DB 写入异常不影响主流程"""
        from agents.group.message_logger import GroupMessageLogger
        logger = MagicMock()
        log = GroupMessageLogger(dialog_uuid="d1", logger=logger)
        mock_save.side_effect = RuntimeError("DB 锁定")

        # 不应抛出异常
        log.error("测试")
        logger.warning.assert_called_once()

    def test_no_progress_no_error(self):
        """无 progress → SSE 推送静默跳过"""
        from agents.group.message_logger import GroupMessageLogger
        log = GroupMessageLogger(progress=None, dialog_uuid="d1")

        with patch("agents.group.message_logger.save_group_message"):
            log.plan([{"step": 1, "agent_names": ["a"], "run_group": 1, "task_purpose": "test"}])

    @patch("agents.group.message_logger.save_group_message")
    def test_step_result_truncates_sse_data(self, mock_save):
        """SSE 推送时 result 被截断到 300 字符"""
        from agents.group.message_logger import GroupMessageLogger
        progress = MagicMock()
        log = GroupMessageLogger(progress=progress, dialog_uuid="d1")

        long_result = "x" * 500
        log.step_result(1, ["a"], "success", long_result)

        sse_data = progress.report.call_args[1].get("data") or progress.report.call_args[0][2]
        if isinstance(sse_data, dict):
            assert len(sse_data.get("result_summary", "")) <= 310  # 300 + "..."

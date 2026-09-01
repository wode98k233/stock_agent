"""CLI Phase 3 测试 — 一次性命令、确认流程"""
import asyncio
from unittest.mock import MagicMock, patch

from cli.commands.base import CommandResult
from cli.context import CLIContext


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(**kwargs) -> CLIContext:
    defaults = dict(
        agent_mode="react_stock",
        skill_register=MagicMock(),
        memory=MagicMock(),
        session_stats=MagicMock(),
    )
    defaults.update(kwargs)
    return CLIContext(**kwargs) if kwargs else CLIContext(**defaults)


class TestOneShotMode:
    def test_handle_input_slash_command(self):
        from cli.repl import handle_input
        ctx = _make_ctx()
        result = _run(handle_input("/help", ctx))
        assert result.ok
        assert "/status" in result.message

    def test_handle_input_natural_language(self):
        """自然语言应走 Agent 路径，mock 掉实际执行。"""
        from cli.repl import handle_input
        ctx = _make_ctx()
        with patch("cli.repl._execute_agent", return_value=CommandResult(ok=True, message="mock result")):
            result = _run(handle_input("分析贵州茅台", ctx))
        assert result.ok
        assert "mock" in result.message


class TestConfirmFlow:
    def test_confirm_result_structure(self):
        """确认流程的 CommandResult 结构。"""
        executed = []
        def action():
            executed.append(True)
            return CommandResult(ok=True, message="已执行")

        r = CommandResult(
            ok=True,
            requires_confirm=True,
            confirm_prompt="确认？",
            pending_action=action,
        )
        assert r.requires_confirm
        assert r.pending_action is not None
        # 手动调用 pending_action
        result = r.pending_action()
        assert result.ok
        assert len(executed) == 1

    def test_confirm_cancel(self):
        """取消时不执行 pending_action。"""
        executed = []
        def action():
            executed.append(True)
            return CommandResult(ok=True, message="已执行")

        r = CommandResult(
            ok=True,
            requires_confirm=True,
            confirm_prompt="确认？",
            pending_action=action,
        )
        # 模拟用户取消 — 不调用 pending_action
        assert r.requires_confirm
        assert len(executed) == 0


class TestReadlineSetup:
    def test_setup_readline_no_crash(self):
        """_setup_readline 不应崩溃，即使 readline 不可用。"""
        from cli.repl import _setup_readline
        # 不应该抛异常
        _setup_readline()

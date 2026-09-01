"""CLI commands 测试"""
import asyncio
from unittest.mock import MagicMock, AsyncMock

from cli.commands.base import CommandResult, CommandHandler
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
    return CLIContext(**defaults)


class TestCommandResult:
    def test_default(self):
        r = CommandResult()
        assert r.ok is True
        assert r.exit is False
        assert r.message == ""

    def test_error(self):
        r = CommandResult(ok=False, message="fail")
        assert r.ok is False
        assert r.message == "fail"


class TestHelpCommand:
    def test_general_help(self):
        from cli.commands.help import HelpCommand
        cmd = HelpCommand()
        ctx = _make_ctx()
        r = _run(cmd.execute(ctx, []))
        assert r.ok
        assert "/status" in r.message

    def test_namespace_help(self):
        from cli.commands.help import HelpCommand
        cmd = HelpCommand()
        ctx = _make_ctx()
        r = _run(cmd.execute(ctx, ["mode"]))
        assert r.ok
        assert "/mode" in r.message

    def test_unknown_namespace(self):
        from cli.commands.help import HelpCommand
        cmd = HelpCommand()
        ctx = _make_ctx()
        r = _run(cmd.execute(ctx, ["nonexistent"]))
        assert r.ok
        assert "未知" in r.message


class TestModeCommand:
    def test_list_modes(self):
        from cli.commands.mode import ModeCommand
        from agents.factory import AgentFactory
        from unittest.mock import patch
        cmd = ModeCommand()
        ctx = _make_ctx()
        with patch.object(AgentFactory, 'list', return_value=["react_stock", "scenario"]):
            r = _run(cmd.execute(ctx, ["list"]))
        assert r.ok
        assert "react_stock" in r.message

    def test_use_mode(self):
        from cli.commands.mode import ModeCommand
        from agents.factory import AgentFactory
        from unittest.mock import patch
        cmd = ModeCommand()
        ctx = _make_ctx()
        with patch.object(AgentFactory, 'list', return_value=["react_stock", "test_mode"]):
            r = _run(cmd.execute(ctx, ["use", "test_mode"]))
        assert r.ok
        assert "test_mode" in r.message

    def test_use_unknown_mode(self):
        from cli.commands.mode import ModeCommand
        from agents.factory import AgentFactory
        from unittest.mock import patch
        cmd = ModeCommand()
        ctx = _make_ctx()
        with patch.object(AgentFactory, 'list', return_value=["react_stock"]):
            r = _run(cmd.execute(ctx, ["use", "nonexistent"]))
        assert not r.ok
        assert "未知" in r.message

    def test_use_no_arg(self):
        from cli.commands.mode import ModeCommand
        cmd = ModeCommand()
        ctx = _make_ctx()
        r = _run(cmd.execute(ctx, ["use"]))
        assert not r.ok


class TestSessionCommand:
    def test_stats(self):
        from cli.commands.session import SessionCommand
        stats = MagicMock()
        stats.start_time = 1000.0
        stats.queries = 5
        stats.total_tokens = 12345
        stats.total_cached_tokens = 0
        stats.total_llm_calls = 10
        stats.total_tool_calls = 3
        cmd = SessionCommand()
        ctx = _make_ctx(session_stats=stats)
        r = _run(cmd.execute(ctx, ["stats"]))
        assert r.ok
        assert "查询次数" in r.message

    def test_clear(self):
        from cli.commands.session import SessionCommand
        memory = MagicMock()
        memory.clear = MagicMock()
        cmd = SessionCommand()
        ctx = _make_ctx(memory=memory)
        r = _run(cmd.execute(ctx, ["clear"]))
        assert r.ok
        memory.clear.assert_called_once()

    def test_exit(self):
        from cli.commands.session import SessionCommand
        cmd = SessionCommand()
        ctx = _make_ctx()
        r = _run(cmd.execute(ctx, ["exit"]))
        assert r.exit is True


class TestStatusCommand:
    def test_status_basic(self):
        from cli.commands.status import StatusCommand
        cmd = StatusCommand()
        ctx = _make_ctx()
        r = _run(cmd.execute(ctx, []))
        assert r.ok
        assert "mode" in r.message
        assert "react_stock" in r.message

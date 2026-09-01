"""CLI Phase 2 命令测试"""
import asyncio
import os
from unittest.mock import MagicMock, patch

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


class TestConfigCommand:
    def test_list(self):
        from cli.commands.config import ConfigCommand
        cmd = ConfigCommand()
        ctx = _make_ctx()
        schema = [{"key": "OPENAI_MODEL_NAME", "label": "模型", "type": "string", "group": "LLM"}]
        with patch("server.config_schema.get_config_schema", return_value=schema):
            with patch.dict(os.environ, {"OPENAI_MODEL_NAME": "gpt-4o"}):
                r = _run(cmd.execute(ctx, ["list"]))
        assert r.ok
        assert "OPENAI_MODEL_NAME" in r.message

    def test_set_requires_confirm(self):
        from cli.commands.config import ConfigCommand
        cmd = ConfigCommand()
        ctx = _make_ctx()
        with patch.dict(os.environ, {"TEST_KEY": "old_value"}):
            r = _run(cmd.execute(ctx, ["set", "TEST_KEY", "new_value"]))
        assert r.requires_confirm
        assert "TEST_KEY" in r.confirm_prompt

    def test_diff(self):
        from cli.commands.config import ConfigCommand
        cmd = ConfigCommand()
        ctx = _make_ctx()
        with patch("utils.env_helper.get_env_diff", return_value={}):
            r = _run(cmd.execute(ctx, ["diff"]))
        assert r.ok
        assert "无差异" in r.message


class TestSkillsCommand:
    def test_list(self):
        from cli.commands.skills import SkillsCommand
        cmd = SkillsCommand()
        meta = MagicMock()
        meta.source = "internal"
        meta.enabled = True
        meta.tools = [{"name": "tool1"}]
        meta.description = "测试技能"
        meta.skill_desc = ""
        reg = MagicMock()
        reg.get_all_skills_unchecked.return_value = {"test_skill": meta}
        ctx = _make_ctx(skill_register=reg)
        r = _run(cmd.execute(ctx, ["list"]))
        assert r.ok
        assert "test_skill" in r.message

    def test_show_not_found(self):
        from cli.commands.skills import SkillsCommand
        cmd = SkillsCommand()
        reg = MagicMock()
        reg.get_skill_detail.return_value = None
        ctx = _make_ctx(skill_register=reg)
        r = _run(cmd.execute(ctx, ["show", "nonexistent"]))
        assert not r.ok

    def test_rescan(self):
        from cli.commands.skills import SkillsCommand
        cmd = SkillsCommand()
        reg = MagicMock()
        reg.get_all_skills.return_value = {"skill1": MagicMock()}
        ctx = _make_ctx(skill_register=reg)
        r = _run(cmd.execute(ctx, ["rescan"]))
        assert r.ok
        reg.rescan.assert_called_once()


class TestTemplatesCommand:
    def test_list(self):
        from cli.commands.templates import TemplatesCommand
        cmd = TemplatesCommand()
        with patch("agents.analysis.template_store._load_index", return_value={
            "default": "standard",
            "templates": {"standard": {"name": "标准分析", "description": "通用分析模板"}},
        }):
            r = _run(cmd.execute(ctx=_make_ctx(), args=["list"]))
        assert r.ok
        assert "standard" in r.message

    def test_validate(self):
        from cli.commands.templates import TemplatesCommand
        cmd = TemplatesCommand()
        template = {"id": "test", "sections": [{"id": "s1", "required": True}]}
        with patch("agents.analysis.template_store._load_index", return_value={
            "templates": {"test": {"name": "测试"}},
        }):
            with patch("agents.analysis.template_store.load_template", return_value=template):
                r = _run(cmd.execute(ctx=_make_ctx(), args=["validate"]))
        assert r.ok
        assert "test" in r.message


class TestNotifyCommand:
    def test_test_no_channel_arg(self):
        from cli.commands.notify import NotifyCommand
        cmd = NotifyCommand()
        r = _run(cmd.execute(ctx=_make_ctx(), args=["test"]))
        assert not r.ok
        assert "用法" in r.message


class TestCalendarCommand:
    def test_today(self):
        from cli.commands.calendar_cmd import CalendarCommand
        cmd = CalendarCommand()
        with patch("utils.trading_calendar.is_market_open", return_value=True):
            r = _run(cmd.execute(ctx=_make_ctx(), args=["today"]))
        assert r.ok
        assert "交易日" in r.message

    def test_month_invalid_format(self):
        from cli.commands.calendar_cmd import CalendarCommand
        cmd = CalendarCommand()
        r = _run(cmd.execute(ctx=_make_ctx(), args=["month", "bad"]))
        assert not r.ok

    def test_month_valid(self):
        from cli.commands.calendar_cmd import CalendarCommand
        cmd = CalendarCommand()
        with patch("server.calendar_service.load_trading_days", return_value=["2026-06-02", "2026-06-03"]):
            r = _run(cmd.execute(ctx=_make_ctx(), args=["month", "2026-06"]))
        assert r.ok
        assert "2026-06" in r.message


class TestWatchlistCommand:
    def test_list_empty(self):
        from cli.commands.watchlist import WatchlistCommand
        cmd = WatchlistCommand()
        with patch.object(cmd, "_get_storage") as mock_storage:
            mock_storage.return_value.list_stocks.return_value = []
            r = _run(cmd.execute(ctx=_make_ctx(), args=["list"]))
        assert r.ok
        assert "为空" in r.message

"""CLI parser 测试"""
from cli.parser import parse_input


class TestParseInput:
    def test_empty_input(self):
        r = parse_input("")
        assert r.kind == "natural"

    def test_natural_language(self):
        r = parse_input("分析一下贵州茅台")
        assert r.kind == "natural"
        assert r.raw == "分析一下贵州茅台"

    def test_slash_command_simple(self):
        r = parse_input("/status")
        assert r.kind == "command"
        assert r.namespace == "status"
        assert r.action == ""

    def test_slash_command_with_action(self):
        r = parse_input("/mode list")
        assert r.kind == "command"
        assert r.namespace == "mode"
        assert r.action == "list"

    def test_slash_command_with_args(self):
        r = parse_input("/mode use react_stock")
        assert r.kind == "command"
        assert r.namespace == "mode"
        assert r.action == "use"
        assert r.args == ["react_stock"]

    def test_slash_command_with_flags(self):
        r = parse_input("/status --json")
        assert r.kind == "command"
        assert r.namespace == "status"
        assert r.flags == {"json": "true"}

    def test_slash_command_with_limit(self):
        r = parse_input("/trace recent --limit 5")
        assert r.kind == "command"
        assert r.namespace == "trace"
        assert r.action == "recent"
        assert r.flags == {"limit": "5"}

    def test_slash_help(self):
        r = parse_input("/help skills")
        assert r.kind == "command"
        assert r.namespace == "help"
        assert r.action == "skills"

    def test_slash_empty(self):
        r = parse_input("/")
        assert r.kind == "command"
        assert r.namespace == "help"

    def test_legacy_exit(self):
        r = parse_input("exit")
        assert r.kind == "legacy"
        assert r.namespace == "session"
        assert r.action == "exit"

    def test_legacy_help(self):
        r = parse_input("help")
        assert r.kind == "legacy"
        assert r.namespace == "help"

    def test_legacy_question_mark(self):
        r = parse_input("?")
        assert r.kind == "legacy"
        assert r.namespace == "help"

    def test_legacy_mode_prefix(self):
        r = parse_input("mode scenario")
        assert r.kind == "legacy"
        assert r.namespace == "mode"
        assert r.action == "use"
        assert r.args == ["scenario"]

    def test_legacy_debug(self):
        r = parse_input("debug")
        assert r.kind == "legacy"
        assert r.namespace == "status"
        assert r.action == "debug"

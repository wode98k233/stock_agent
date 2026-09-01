import asyncio


def test_replay_history_appends_only_complete_turns():
    from server.agent_runner import WebAgentContext

    class FakeMemory:
        enabled = True

        def __init__(self):
            self.turns = []

        def append_turn(self, user_text, assistant_text):
            self.turns.append((user_text, assistant_text))

    class FakeStorage:
        def list_messages(self, dialog_uuid):
            assert dialog_uuid == "dialog-1"
            return [
                {"role": "user", "content": "question 1"},
                {"role": "assistant", "content": "answer 1", "status": "completed"},
                {"role": "user", "content": "question 2"},
                {"role": "assistant", "content": "answer 2", "status": "completed"},
                {"role": "user", "content": "current question"},
            ]

    memory = FakeMemory()

    WebAgentContext._replay_history(memory, "dialog-1", FakeStorage())

    assert memory.turns == [
        ("question 1", "answer 1"),
        ("question 2", "answer 2"),
    ]


def test_run_web_agent_query_uses_explicit_mode_without_changing_default(monkeypatch):
    asyncio.run(_run_web_agent_query_uses_explicit_mode_without_changing_default(monkeypatch))


async def _run_web_agent_query_uses_explicit_mode_without_changing_default(monkeypatch):
    from agents.factory import AgentFactory
    from server.agent_runner import WebAgentContext, run_web_agent_query
    from utils.progress import ProgressType

    calls = []

    class FakeAgent:
        name = "pdor"

        def on_startup(self):
            calls.append(("startup", self.name))

        async def run(self, user_input, registry, memory, logger, progress_callback=None, **kwargs):
            calls.append(("run", user_input, registry.memory_mgr, logger))
            progress_callback and progress_callback(
                __import__("utils.progress", fromlist=["ProgressEvent"]).ProgressEvent(
                    ProgressType.START,
                    "开始",
                    timestamp=1.0,
                )
            )
            return "模拟 Agent 回答"

    class FakeRegistry:
        def __init__(self, logger, memory_mgr, register):
            self.logger = logger
            self.memory_mgr = memory_mgr
            self.register = register

    def fake_get(cls, name=None):
        calls.append(("get", name))
        return FakeAgent()

    monkeypatch.setattr(AgentFactory, "_current", "react_stock")
    monkeypatch.setattr(AgentFactory, "get", classmethod(fake_get))
    monkeypatch.setattr("server.agent_runner.SkillRegistry", FakeRegistry)
    class FakeLogger:
        def info(self, *a, **kw):
            pass
        def warning(self, *a, **kw):
            pass
        def error(self, *a, **kw):
            pass
        def debug(self, *a, **kw):
            pass

    monkeypatch.setattr(
        "server.agent_runner.get_logger",
        lambda user_input, **_: (FakeLogger(), "log-uuid", object(), "logs/2026-06-04/test.log"),
    )
    monkeypatch.setattr("server.agent_runner.close_logger", lambda logger: calls.append(("close", logger)))

    stats = type("Stats", (), {"accumulate": lambda self, ctx: calls.append(("stats", ctx))})()
    context = WebAgentContext(skill_register="register", session_stats=stats)
    events = []

    result = await run_web_agent_query(
        user_input="分析贵州茅台",
        mode="pdor",
        context=context,
        progress_callback=events.append,
    )

    assert result.response == "模拟 Agent 回答"
    assert result.log_uuid == "log-uuid"
    assert result.log_file == "logs/2026-06-04/test.log"
    assert AgentFactory._current == "react_stock"
    assert calls[0] == ("get", "pdor")
    assert calls[1] == ("startup", "pdor")
    assert calls[2][0] == "run"
    assert calls[2][1] == "分析贵州茅台"
    assert calls[-1][0] == "close"
    assert len(events) == 1
    assert events[0].type == ProgressType.START


# ══════════════════════════════════════════════════════════════
# _archive_memory 门禁测试
# ══════════════════════════════════════════════════════════════

def test_archive_memory_skips_short_responses():
    """响应太短（< 200 字符）时跳过归档，防止错误消息被 chunk 入库。"""
    from server.agent_runner import _archive_memory

    calls = {}

    class FakeSDK:
        def archive(self, *a, **kw):
            calls["archived"] = True

    class FakeBackend:
        def name(self):
            return "fake"
        def count(self):
            return 0

    class FakeContext:
        memory_sdk = FakeSDK()

    sdk = FakeSDK()
    sdk.backend = FakeBackend()
    FakeContext.memory_sdk = sdk

    logger = type("Log", (), {
        "info": lambda *a: calls.setdefault("logs", []).append(a),
        "warning": lambda *a: None,
        "error": lambda *a: None,
    })()

    # 短响应 → 跳过
    _archive_memory(FakeContext(), "用户问题", "太短了", "uuid", logger, None)
    assert "archived" not in calls
    assert any("too short" in str(a) for a in calls.get("logs", []))


def test_archive_memory_skips_error_prefix_responses():
    """以错误前缀开头的响应跳过归档。"""
    from server.agent_runner import _archive_memory

    calls = {}

    class FakeSDK:
        def archive(self, *a, **kw):
            calls["archived"] = True

    class FakeBackend:
        def name(self):
            return "fake"
        def count(self):
            return 0

    class FakeContext:
        memory_sdk = FakeSDK()

    sdk = FakeSDK()
    sdk.backend = FakeBackend()
    FakeContext.memory_sdk = sdk

    logger = type("Log", (), {
        "info": lambda *a: calls.setdefault("logs", []).append(a),
        "warning": lambda *a: None,
        "error": lambda *a: None,
    })()

    # 以错误前缀开头的长响应也跳过
    long_error = "分析过程出错：" + "x" * 500
    _archive_memory(FakeContext(), "用户问题", long_error, "uuid", logger, None)
    assert "archived" not in calls


def test_archive_memory_allows_valid_long_responses():
    """正常的长响应应正常归档。"""
    from server.agent_runner import _archive_memory

    calls = {}

    class FakeSDK:
        def archive(self, *a, **kw):
            calls["archived"] = True

    class FakeBackend:
        def name(self):
            return "fake"
        def count(self):
            return 0

    class FakeContext:
        memory_sdk = FakeSDK()

    sdk = FakeSDK()
    sdk.backend = FakeBackend()
    FakeContext.memory_sdk = sdk

    logger = type("Log", (), {
        "info": lambda *a: None,
        "warning": lambda *a: None,
        "error": lambda *a: None,
    })()

    valid_response = "## 核心结论\n" + "今天大盘全面下跌。" * 50  # 远超 200 字符
    _archive_memory(FakeContext(), "用户问题", valid_response, "uuid", logger, None)
    assert calls.get("archived") is True

"""会话标题（classify 生成 → 落库 → SSE 推送）单测。"""
import asyncio
from types import SimpleNamespace


def _fake_logger():
    return type("Log", (), {
        "isEnabledFor": lambda *a, **kw: False,
        "debug": lambda *a, **kw: None,
        "info": lambda *a, **kw: None,
        "warning": lambda *a, **kw: None,
        "error": lambda *a, **kw: None,
    })()


# ══════════════════════════════════════════════════════════════
# storage：占位标题不锁定 / LLM 标题落库
# ══════════════════════════════════════════════════════════════

def test_touch_dialog_writes_placeholder_without_locking(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")
    assert dialog["title_auto"] == 0

    storage.touch_dialog_for_message(
        dialog["dialog_uuid"], "react_stock", "分析红利低波ETF近期下跌原因")
    touched = storage.get_dialog(dialog["dialog_uuid"])
    assert touched["title"] == "分析红利低波ETF近期下跌原因"
    assert touched["title_auto"] == 0  # 占位标题可被 LLM 标题替换

    storage.update_dialog(
        dialog["dialog_uuid"], title="红利低波ETF 下跌归因", title_auto=1)
    final = storage.get_dialog(dialog["dialog_uuid"])
    assert final["title"] == "红利低波ETF 下跌归因"
    assert final["title_auto"] == 1


# ══════════════════════════════════════════════════════════════
# classify_input：标题生成、清洗与回调
# ══════════════════════════════════════════════════════════════

def _run_classify(monkeypatch, fake_result, *, first_question=None, memory=None):
    """执行 classify_input，返回 (result, captured_messages, titles)。"""
    from agents import common
    from agents.shared.title_ctx import TitleRequest, reset_title_request, set_title_request

    captured = {}
    titles = []

    def fake_llm_json(llm, messages, logger, **kwargs):
        captured["messages"] = messages
        return fake_result

    monkeypatch.setattr("utils.llm_factory.llm_json_with_retry", fake_llm_json)

    if memory is None:
        memory = SimpleNamespace(get_history=lambda: [], append_turn=lambda *a: None)

    token = None
    if first_question is not None:
        token = set_title_request(
            TitleRequest(first_question=first_question, callback=titles.append))
    try:
        result = asyncio.run(common.classify_input(
            "本轮提问", memory, object(), _fake_logger()))
    finally:
        if token is not None:
            reset_title_request(token)
    return result, captured["messages"], titles


def test_classify_with_title_request_emits_sanitized_title(monkeypatch):
    result, messages, titles = _run_classify(
        monkeypatch,
        {"is_stock_related": True, "response": "", "title": "\n「红利低波ETF 下跌归因」\n"},
        first_question="红利低波ETF为什么跌",
    )

    assert titles == ["红利低波ETF 下跌归因"]
    assert result["title"] == "红利低波ETF 下跌归因"
    system_prompt = messages[0].content
    assert "附加任务" in system_prompt
    assert "红利低波ETF为什么跌" in system_prompt


def test_classify_without_title_request_skips_title_prompt(monkeypatch):
    result, messages, _ = _run_classify(
        monkeypatch,
        {"is_stock_related": True, "response": "", "title": "不应生效"},
    )

    assert "附加任务" not in messages[0].content
    assert result == {}  # 股票相关且无标题请求 → 保持空 dict 语义


def test_classify_non_stock_returns_title_and_appends_turn(monkeypatch):
    turns = []
    memory = SimpleNamespace(
        get_history=lambda: [], append_turn=lambda u, r: turns.append((u, r)))

    result, _, titles = _run_classify(
        monkeypatch,
        {"is_stock_related": False, "response": "抱歉，我专注于股票和金融分析。",
         "title": "闲聊：天气"},
        first_question="今天天气怎么样",
        memory=memory,
    )

    assert result["response"] == "抱歉，我专注于股票和金融分析。"
    assert result["title"] == "闲聊：天气"
    assert titles == ["闲聊：天气"]
    assert turns == [("本轮提问", "抱歉，我专注于股票和金融分析。")]


def test_sanitize_title_rules():
    from agents.common import _sanitize_title

    assert _sanitize_title("  红利低波ETF 下跌归因。  ") == "红利低波ETF 下跌归因"
    assert _sanitize_title('"引号包裹"') == "引号包裹"
    assert _sanitize_title("第一行\n第二行") == "第一行 第二行"
    assert _sanitize_title("x" * 50) == "x" * 20
    assert _sanitize_title("") is None
    assert _sanitize_title(None) is None


# ══════════════════════════════════════════════════════════════
# coordinator：title 事件、落库时序与一次性口径
# ══════════════════════════════════════════════════════════════

def test_task_runtime_emits_title_and_persists(tmp_path):
    asyncio.run(_task_runtime_emits_title_and_persists(tmp_path))


async def _task_runtime_emits_title_and_persists(tmp_path):
    from server.agent_runner import AgentRunResult
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")
    calls = []

    async def runner(*, user_input, mode, context, progress_callback,
                     title_callback=None, title_source=""):
        calls.append({"title_source": title_source, "title_callback": title_callback})
        if title_callback:
            title_callback("红利低波ETF 下跌归因")
        return AgentRunResult(response="完成", log_uuid="log-1", log_file=None, trace_run_id=None)

    async def run_once(runtime, content):
        submitted = await runtime.submit_message(
            storage=storage, context=SimpleNamespace(),
            dialog_uuid=dialog["dialog_uuid"], content=content, mode="react_stock",
        )
        events = []
        async for payload in runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01):
            events.append(payload)
            if payload["event"] in ("final", "task_error"):
                break
        return events

    runtime = TaskRuntime(runner)

    # 首轮：以首问为依据生成标题 → 落库 + SSE title 事件
    events = await run_once(runtime, "分析红利低波ETF为什么跌")
    assert calls[0]["title_source"] == "分析红利低波ETF为什么跌"
    title_events = [e for e in events if e["event"] == "title"]
    assert len(title_events) == 1
    assert title_events[0]["data"]["dialog_uuid"] == dialog["dialog_uuid"]
    assert title_events[0]["data"]["title"] == "红利低波ETF 下跌归因"

    saved = storage.get_dialog(dialog["dialog_uuid"])
    assert saved["title"] == "红利低波ETF 下跌归因"
    assert saved["title_auto"] == 1

    # 回调二次触发被 title_auto 守卫拦截（成功后永不覆盖）
    calls[0]["title_callback"]("不应覆盖")
    assert storage.get_dialog(dialog["dialog_uuid"])["title"] == "红利低波ETF 下跌归因"

    # 第二轮：已生成过 → 不再注入 title_callback/title_source，无 title 事件
    events2 = await run_once(runtime, "那现在还能买吗")
    assert calls[1]["title_source"] == ""
    assert calls[1]["title_callback"] is None
    assert [e for e in events2 if e["event"] == "title"] == []


def test_task_runtime_retries_title_before_success(tmp_path):
    """首轮无标题（LLM 未返回）→ title_auto 保持 0，次轮以首问补生成。"""
    asyncio.run(_task_runtime_retries_title_before_success(tmp_path))


async def _task_runtime_retries_title_before_success(tmp_path):
    from server.agent_runner import AgentRunResult
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")
    calls = []

    async def runner(*, user_input, mode, context, progress_callback,
                     title_callback=None, title_source=""):
        calls.append(title_source)
        if title_callback and len(calls) > 1:  # 首轮模拟失败，次轮补生成
            title_callback("红利低波ETF 下跌归因")
        return AgentRunResult(response="完成", log_uuid="log-1", log_file=None, trace_run_id=None)

    async def run_once(runtime, content):
        submitted = await runtime.submit_message(
            storage=storage, context=SimpleNamespace(),
            dialog_uuid=dialog["dialog_uuid"], content=content, mode="react_stock",
        )
        async for payload in runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01):
            if payload["event"] in ("final", "task_error"):
                break

    runtime = TaskRuntime(runner)
    await run_once(runtime, "分析红利低波ETF为什么跌")
    assert storage.get_dialog(dialog["dialog_uuid"])["title_auto"] == 0

    await run_once(runtime, "顺便看看成交量")
    # 补生成仍以首轮提问为依据，而非当前提问
    assert calls[1] == "分析红利低波ETF为什么跌"
    saved = storage.get_dialog(dialog["dialog_uuid"])
    assert saved["title"] == "红利低波ETF 下跌归因"
    assert saved["title_auto"] == 1


# ══════════════════════════════════════════════════════════════
# agent_runner：TitleRequest 注入与清理
# ══════════════════════════════════════════════════════════════

def test_run_web_agent_query_injects_title_request(monkeypatch):
    asyncio.run(_run_web_agent_query_injects_title_request(monkeypatch))


async def _run_web_agent_query_injects_title_request(monkeypatch):
    from agents.factory import AgentFactory
    from agents.shared.title_ctx import get_title_request
    from server.agent_runner import WebAgentContext, run_web_agent_query

    seen = {}

    class FakeAgent:
        name = "react_stock"

        def on_startup(self):
            pass

        async def run(self, user_input, registry, memory, logger,
                      progress_callback=None, **kwargs):
            req = get_title_request()
            seen["first_question"] = req.first_question if req else None
            if req:
                req.callback("红利低波ETF 下跌归因")
            return "短回答"

    class FakeRegistry:
        def __init__(self, logger, memory_mgr, register):
            self.memory_mgr = memory_mgr

    monkeypatch.setattr(AgentFactory, "_current", "react_stock")
    monkeypatch.setattr(AgentFactory, "get", classmethod(lambda cls, name=None: FakeAgent()))
    monkeypatch.setattr("server.agent_runner.SkillRegistry", FakeRegistry)
    monkeypatch.setattr(
        "server.agent_runner.get_logger",
        lambda user_input, **_: (_fake_logger(), "log-uuid", object(), "logs/test.log"),
    )
    monkeypatch.setattr("server.agent_runner.close_logger", lambda logger: None)

    context = WebAgentContext(skill_register="register")
    context.get_memory = lambda dialog_uuid, storage: SimpleNamespace(enabled=False)

    titles = []
    await run_web_agent_query(
        user_input="分析红利低波ETF为什么跌",
        mode="react_stock",
        context=context,
        title_callback=titles.append,
        title_source="分析红利低波ETF为什么跌",
    )

    assert seen["first_question"] == "分析红利低波ETF为什么跌"
    assert titles == ["红利低波ETF 下跌归因"]
    assert get_title_request() is None  # 请求结束后 ContextVar 已清理

    # 不传 title_callback 时不注入（CLI/TUI 等入口零影响）
    await run_web_agent_query(
        user_input="分析贵州茅台",
        mode="react_stock",
        context=context,
    )
    assert seen["first_question"] is None

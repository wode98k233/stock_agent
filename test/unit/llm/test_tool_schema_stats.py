"""任务 8 / 8b：工具 schema 体积埋点（tools_count / tools_chars / tools_measured）。

⚠️ 铁律：前两个字段是**明细不是增量** —— 工具定义的 token 本来就含在 provider
返回的 input_tokens 里（schema 是随请求发出去的）。它们只用于把「工具定义」从输入侧
**切分**出来给环形图用，**绝不**加到总量、**绝不**参与计价（加了就是重复计费）。
本文件后半部分（*_does_not_touch_totals 系列）就是钉住这条铁律的。

tools_measured 是**显式哨兵**：`tools_chars == 0` 既可能是「真没绑工具」（实测 0），
也可能是「这条路径没被埋点」（流式 / response_format / responses API 分支都不注入），
二者同形 —— 靠推断区分状态正是 bug 藏身之处，故由本模块显式写哨兵，消费侧据此给
None（未采集）或数字。本文件只钉**生产侧**（写与透传），消费侧见
test/unit/trace/test_context_stats.py。
"""
import asyncio
import copy
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import HumanMessage

from utils.llm_factory import (
    TokenCompatibleChatOpenAI,
    _attach_tool_schema_stats,
    tool_schema_stats,
)
from utils.token_usage import extract_token_usage, normalize_token_usage_dict


# ── 构造工具 ────────────────────────────────────────────────────────────────

def _one_tool():
    return [{"type": "function", "function": {"name": "iwc_query",
             "description": "x" * 100, "parameters": {"type": "object"}}}]


def _two_tools():
    return [
        {"type": "function", "function": {"name": "iwc_query", "description": "x" * 100,
                                          "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "iwc_search", "description": "y" * 50,
                                          "parameters": {"type": "object"}}},
    ]


_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "hi"}}],
    # provider 回的 input 里**已经含**工具 schema 的 token，这里不额外加
    "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020},
}


def _llm(**kwargs):
    # 构造离线实例：只造 client，不发请求（真实注入路径照常走）
    return TokenCompatibleChatOpenAI(
        model="gpt-4o-mini",
        api_key="sk-test",
        base_url="https://example.invalid/v1",
        **kwargs,
    )


def _stub_result():
    """父类分支返回的 ChatResult 替身（带 token_usage、**不带** tools_*）。"""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    return ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="x"))],
        llm_output={"token_usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                    "total_tokens": 6}},
    )


def _fake_raw():
    # ⚠️ 必须 deepcopy：LangChain 的 _create_chat_result 把 response["usage"] **同一个 dict**
    # 直接挂进 llm_output["token_usage"]，注入会写回这个对象；共用模块级常量会让
    # 前一个用例的 tools_count 污染后一个用例（本轮实测踩到：无工具的用例读到 2）。
    response = copy.deepcopy(_COMPLETION)
    raw = MagicMock()
    raw.parse.return_value = response
    raw.http_response.json.return_value = response
    raw.headers = {}
    return raw


@contextmanager
def _patched_sync_client(llm):
    """把底层 HTTP 调用换成假响应；_generate 的注入路径原样执行。"""
    wrapper = MagicMock()
    wrapper.create.return_value = _fake_raw()
    with patch.object(type(llm.client), "with_raw_response",
                      new_callable=PropertyMock) as prop:
        prop.return_value = wrapper
        yield wrapper


@contextmanager
def _patched_async_client(llm):
    wrapper = MagicMock()
    wrapper.create = AsyncMock(return_value=_fake_raw())
    with patch.object(type(llm.async_client), "with_raw_response",
                      new_callable=PropertyMock) as prop:
        prop.return_value = wrapper
        yield wrapper


class _FakeResult:
    """只带 llm_output 的 LLMResult 替身（extract_token_usage 需要的唯一入口）。"""

    def __init__(self, llm_output):
        self.llm_output = llm_output


class _CapturingHandler(BaseCallbackHandler):
    """抓回调里的 LLMResult.llm_output —— 验证 ChatResult.llm_output 被 LangChain 提升。"""

    def __init__(self):
        self.llm_output = None

    def on_llm_end(self, response, **kwargs):
        self.llm_output = getattr(response, "llm_output", None)


# ── tool_schema_stats：量体积 ──────────────────────────────────────────────

def test_tool_schema_stats_counts_chars():
    stats = tool_schema_stats(_one_tool())
    assert stats["tools_count"] == 1
    # description 就有 100 字符，序列化后必然更多（含 name/parameters 等）
    assert stats["tools_chars"] > 100


def test_tool_schema_stats_empty():
    assert tool_schema_stats(None) == {"tools_count": 0, "tools_chars": 0}
    assert tool_schema_stats([]) == {"tools_count": 0, "tools_chars": 0}


def test_tool_schema_stats_falls_back_to_str_when_not_serializable():
    """不可 JSON 序列化的 schema（工具对象/含循环引用）走 str 降级，不能抛异常。

    真实产出可达性：build_all_langchain_tools 交付的是 dict 化后的 schema
    （payload["tools"]），所以这里用**合成输入**钉住降级分支。
    """
    obj = object()
    stats = tool_schema_stats([obj])
    assert stats["tools_count"] == 1
    assert stats["tools_chars"] == len(str(obj))


# ── token_usage：缺省 0 / 透传 / 不动总量 ──────────────────────────────────

def test_zero_usage_carries_new_fields():
    usage = normalize_token_usage_dict(None)
    assert usage["tools_count"] == 0
    assert usage["tools_chars"] == 0


def test_tools_measured_defaults_false_and_passes_through():
    """哨兵默认假（未采集），一旦上游写了真就原样透传（两个抽取路径都要）。"""
    assert normalize_token_usage_dict(None)["tools_measured"] is False
    assert normalize_token_usage_dict({"input_tokens": 1})["tools_measured"] is False
    assert normalize_token_usage_dict(
        {"input_tokens": 1, "tools_measured": True})["tools_measured"] is True

    class _Result:
        llm_output = {"token_usage": {"input_tokens": 1, "tools_measured": True}}

    assert extract_token_usage(_Result())["tools_measured"] is True
    # 非真值（脏数据）不得被当成「已采集」
    assert normalize_token_usage_dict(
        {"input_tokens": 1, "tools_measured": 0})["tools_measured"] is False


def test_extract_tools_measured_absent_defaults_false():
    """`_extract_from_dict` 的**缺席默认**也要直断言。

    这是**流式路径的真实形态**：流式不经过 _attach_tool_schema_stats，provider 回报的
    token_usage 里根本没有 tools_measured —— 必须落到 False（未采集），不能落到真值。
    只测 normalize 侧抓不到这条（两个抽取路径各有一份默认值）。
    """

    class _StreamLike:
        llm_output = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                      "tools_count": 0, "tools_chars": 0}}

    usage = extract_token_usage(_StreamLike())
    assert usage["tools_measured"] is False
    assert usage["tools_chars"] == 0          # 0 是缺省值，不是实测值（哨兵说它没被埋点）

    # 字符串 "false" 同样是脏数据：非 True 一律未采集（fail-safe，见 token_usage._as_int 上方注释）
    class _Dirty:
        llm_output = {"token_usage": {"prompt_tokens": 1, "tools_measured": "false"}}

    assert extract_token_usage(_Dirty())["tools_measured"] is False

    # 脏 tools_chars（自由 JSON 里落下的非数字）同样不得抛 —— 两个抽取路径各有一份
    # 默认值/容错，只测 normalize 侧会漏掉这一份（变异 M4b 实测：去掉它无人报警）。
    class _DirtyChars:
        llm_output = {"token_usage": {"prompt_tokens": 1, "tools_chars": "n/a",
                                      "tools_measured": True}}

    dirty = extract_token_usage(_DirtyChars())
    assert dirty["tools_chars"] == 0          # 脏值读成 0，但不改变「已采集」的事实
    assert dirty["tools_measured"] is True


def test_attach_sets_measured_sentinel_even_with_zero_tools():
    """走到注入点就是「已埋点」—— 真绑 0 个工具也必须标真（0 是实测值，不是未采集）。"""
    result = _FakeResult({"token_usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    _attach_tool_schema_stats(result, {"tools_count": 0, "tools_chars": 0})
    tu = result.llm_output["token_usage"]
    assert tu["tools_measured"] is True
    assert tu["tools_count"] == 0 and tu["tools_chars"] == 0


def test_extract_picks_up_new_fields():
    class _Msg:
        response_metadata = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                            "tools_count": 52, "tools_chars": 53000}}

    class _Gen:
        message = _Msg()

    class _Result:
        generations = [[_Gen()]]
        llm_output = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                      "tools_count": 52, "tools_chars": 53000}}

    usage = extract_token_usage(_Result())
    assert usage["tools_count"] == 52
    assert usage["tools_chars"] == 53000
    # 铁律：新字段是明细，不得改变 input/output
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 2
    assert usage["total_tokens"] == 12      # 10 + 2，不含 53000 个字符


def test_normalize_carries_new_fields_without_touching_totals():
    usage = normalize_token_usage_dict({"prompt_tokens": 10, "completion_tokens": 2,
                                        "tools_count": 52, "tools_chars": 53000})
    assert usage["tools_count"] == 52
    assert usage["tools_chars"] == 53000
    # 铁律：明细既不进 input 也不进 total
    assert usage["input_tokens"] == 10
    assert usage["total_tokens"] == 12


def test_normalize_zero_defaults_when_missing():
    usage = normalize_token_usage_dict({"prompt_tokens": 10, "completion_tokens": 2})
    assert usage["tools_count"] == 0
    assert usage["tools_chars"] == 0


# ── _attach_tool_schema_stats：注入 ChatResult.llm_output ─────────────────

def test_attach_injects_into_token_usage():
    result = _FakeResult({"token_usage": {"prompt_tokens": 1000, "completion_tokens": 20,
                                          "total_tokens": 1020}})
    _attach_tool_schema_stats(result, {"tools_count": 52, "tools_chars": 53000})

    tu = result.llm_output["token_usage"]
    assert tu["tools_count"] == 52
    assert tu["tools_chars"] == 53000
    # 铁律：注入只做并集，不动 input/output/total
    assert tu["total_tokens"] == 1020


def test_attach_creates_token_usage_when_absent():
    result = _FakeResult({})
    _attach_tool_schema_stats(result, {"tools_count": 2, "tools_chars": 150})
    assert result.llm_output["token_usage"]["tools_chars"] == 150


def test_attach_keeps_existing_value():
    """setdefault 语义：provider/上游已给值时不覆盖。"""
    result = _FakeResult({"token_usage": {"tools_count": 7, "tools_chars": 701}})
    _attach_tool_schema_stats(result, {"tools_count": 52, "tools_chars": 53000})
    assert result.llm_output["token_usage"]["tools_count"] == 7
    assert result.llm_output["token_usage"]["tools_chars"] == 701


def test_attach_skips_when_llm_output_missing_or_not_dict():
    no_output = _FakeResult(None)
    assert _attach_tool_schema_stats(no_output, {"tools_count": 1, "tools_chars": 5}) is no_output
    assert no_output.llm_output is None

    bad_usage = _FakeResult({"token_usage": "not-a-dict"})
    _attach_tool_schema_stats(bad_usage, {"tools_count": 1, "tools_chars": 5})
    assert bad_usage.llm_output["token_usage"] == "not-a-dict"


# ── 生产路径：_generate / _agenerate 真的注入 ──────────────────────────────

def test_generate_injects_tool_schema_stats():
    llm = _llm()
    with _patched_sync_client(llm):
        result = llm._generate([("user", "hi")], stop=None, tools=_two_tools())

    tu = result.llm_output["token_usage"]
    assert tu["tools_count"] == 2
    assert tu["tools_chars"] > 150
    # 哨兵必须一起写：只写 count/chars 的话，「有工具」与「已埋点」会被当成同一件事，
    # 日后给注入加一道 `if tools:` 门（无工具就不写）也没人发现。
    assert tu["tools_measured"] is True
    # 铁律：provider 报的用量原样保留
    assert tu["prompt_tokens"] == 1000
    assert tu["completion_tokens"] == 20
    assert tu["total_tokens"] == 1020

    usage = extract_token_usage(result)
    assert usage["tools_chars"] == tu["tools_chars"]
    assert usage["input_tokens"] == 1000 and usage["total_tokens"] == 1020


def test_measured_sentinel_survives_into_consumer_path():
    """生产→消费闭环：_generate 注入的 usage 经 extract/normalize 后，消费侧判为「已采集」
    并给出数字（而不是 None）。这条链一旦断（比如注入忘了写哨兵），UI 就永远显示「未采集」。
    """
    from utils.agent_trace import context_stats

    llm = _llm()
    with _patched_sync_client(llm):
        result = llm._generate([("user", "hi")], stop=None, tools=_two_tools())

    usage = normalize_token_usage_dict(extract_token_usage(result))
    assert usage["tools_measured"] is True
    assert context_stats._tools_tokens(usage) == context_stats.estimate_tokens(
        "x" * usage["tools_chars"])
    assert context_stats._tools_tokens(usage) > 0


def test_generate_without_tools_reports_zero():
    """无工具的调用如实记 0（**不是「未采集」**）—— 这正是哨兵要防的两义混淆。

    变异验证：给注入加一道 `if tools:` 门（无工具就不写哨兵）后本用例立刻红 ——
    它断言的是「0 且 tools_measured is True」，只看 count/chars 的话这种退化照样绿。
    """
    llm = _llm()
    with _patched_sync_client(llm):
        result = llm._generate([("user", "hi")], stop=None)

    tu = result.llm_output["token_usage"]
    assert tu["tools_count"] == 0
    assert tu["tools_chars"] == 0
    assert tu["tools_measured"] is True   # 0 是**实测值**，不是「没埋点」


def test_agenerate_injects_tool_schema_stats():
    llm = _llm()

    async def run():
        with _patched_async_client(llm):
            return await llm._agenerate([("user", "hi")], stop=None, tools=_two_tools())

    result = asyncio.run(run())
    tu = result.llm_output["token_usage"]
    assert tu["tools_count"] == 2
    assert tu["tools_chars"] > 150
    assert tu["tools_measured"] is True   # 异步路径同样要写哨兵
    assert tu["total_tokens"] == 1020


def test_promotion_through_callbacks_feeds_extract_token_usage():
    """链路闭环：ChatResult.llm_output →(LangChain 提升)→ LLMResult.llm_output
    → extract_token_usage 取到 tools_*。这就是「不改 bind_tools 也能拿到」的证据。"""
    llm = _llm()
    cap = _CapturingHandler()
    with _patched_sync_client(llm):
        llm.generate([[HumanMessage(content="promotion-check")]],
                     tools=_two_tools(), callbacks=[cap])

    assert cap.llm_output is not None
    usage = extract_token_usage(_FakeResult(cap.llm_output))
    assert usage["tools_count"] == 2
    assert usage["tools_chars"] > 150
    # 铁律：明细不改总量
    assert usage["input_tokens"] == 1000 and usage["total_tokens"] == 1020


def test_bind_tools_forwards_tools_as_generate_kwarg():
    """设计前提：bind_tools 不改 _generate，只是把 tools 作为 kwargs 传下来。

    这正是「不需要覆写 bind_tools、在 _generate 里从 payload 量尺寸」的依据：
    克隆体上的属性 _generate 读不到，但 tools 这个 kwarg 一定进 payload。
    """
    llm = _llm()
    bound = llm.bind_tools(_two_tools())
    assert bound.kwargs.get("tools") == _two_tools()

    # 顺着克隆体实际走的赋值方式调用一次 _generate（等价于 RunnableBinding 的委托）
    with _patched_sync_client(llm):
        result = bound.bound._generate([("user", "hi")], **bound.kwargs)
    assert result.llm_output["token_usage"]["tools_count"] == 2


def test_response_format_branch_is_not_instrumented():
    """覆盖边界（有意为之，非漏采）：_generate 走 response_format 的提前 return
    分支**不注入** —— 该次调用 tools_measured 缺省假，消费侧给 None（「未采集」），
    不假装覆盖全。"""
    from langchain_openai import ChatOpenAI

    stub = _stub_result()
    llm = _llm()
    with patch.object(ChatOpenAI, "_generate", return_value=stub) as m:
        result = llm._generate([("user", "hi")], tools=_two_tools(),
                               response_format={"type": "json_object"})
    assert m.called, "response_format 分支应直接委托父类，不再自己发请求"
    assert result is stub
    assert "tools_count" not in result.llm_output["token_usage"]
    assert "tools_measured" not in result.llm_output["token_usage"]


def test_agenerate_response_format_branch_is_not_instrumented():
    """覆盖边界：**_agenerate** 的同类提前 return 分支同样不注入（与同步分支同规格，
    不能只钉同步那条 —— 异步是网关的默认路径）。"""
    from langchain_openai import ChatOpenAI

    stub = _stub_result()
    llm = _llm()
    parent = AsyncMock(return_value=stub)
    with patch.object(ChatOpenAI, "_agenerate", parent):
        result = asyncio.run(llm._agenerate([("user", "hi")], tools=_two_tools(),
                                           response_format={"type": "json_object"}))
    assert parent.await_count == 1, "response_format 分支应直接委托父类"
    assert result is stub
    assert "tools_count" not in result.llm_output["token_usage"]
    assert "tools_measured" not in result.llm_output["token_usage"]


def test_responses_api_branch_is_not_instrumented():
    """覆盖边界：`_use_responses_api(payload)` 这个**子条件**也要钉。

    用真实配置开关 use_responses_api=True —— 该属性本身是 bool 时，父类的
    `_use_responses_api` 会直接返回它（不依赖模型名/其它 payload 特征），而不是
    monkeypatch 掉那个方法；这样钉的是真实可达路径，也不钉第三方行号（升级即失真）。
    """
    from langchain_openai import ChatOpenAI

    stub = _stub_result()
    llm = _llm(use_responses_api=True)
    with patch.object(ChatOpenAI, "_generate", return_value=stub) as m:
        result = llm._generate([("user", "hi")], tools=_two_tools())
    assert m.called, "use_responses_api=True 时应走父类的 responses API 分支"
    assert result is stub
    assert "tools_count" not in result.llm_output["token_usage"]
    assert "tools_measured" not in result.llm_output["token_usage"]


def test_generate_still_returns_created_chat_result():
    """回归护栏：注入点必须在 _generate 最后一行，不能抢在 _create_chat_result 之前
    （否则 reasoning_content 回填/tool_calls 解析等既有逻辑会被绕过）。"""
    llm = _llm()
    with _patched_sync_client(llm):
        result = llm._generate([("user", "hi")], stop=None, tools=_two_tools())

    message = result.generations[0].message
    assert message.content == "hi"
    assert "reasoning_content" not in message.additional_kwargs  # 非 thinking 供应商


def test_tools_chars_does_not_change_cost():
    """铁律：明细不参与计价 —— 同一份用量多带 tools_*，成本必须一分不差。

    直接用计价内核 calc_cost（与 TokenRecorder / TraceRecorder 同一函数），
    合成一份价目，绕开运行时价目文件的可用性差异。

    ⚠️ 本用例是**接近同义反复**的窄护栏：calc_cost 只读 input/output/cached 三个具名键
    （utils/pricing.py::split_tokens），所以它只能抓「计价侧显式去读 tools_chars」这一种
    （不可能发生的）改法。**注入侧**（把 tools_chars 混进 input_tokens / total_tokens）
    它一律抓不到 —— 那一侧由 test_attach_injects_into_token_usage 与
    test_generate_injects_tool_schema_stats 承担（两者都断言 input/total 原样不变）。
    勿把本用例当成唯一护栏。
    """
    from utils.pricing import PriceQuote, calc_cost

    quote = PriceQuote(
        provider="probe", model="probe-model", billing="paid",
        prices={"off_peak": {"in_miss": 1.0, "in_hit": 0.1, "out": 2.0},
                "peak": {"in_miss": 2.0, "in_hit": 0.2, "out": 4.0}},
        schedule={},
    )
    base = {"input_tokens": 528, "output_tokens": 2, "cached_tokens": 0}
    with_tools = dict(base, tools_count=2, tools_chars=620)

    cost_base = calc_cost(base, quote, ts=0)
    cost_tools = calc_cost(with_tools, quote, ts=0)
    assert cost_base.total is not None and cost_base.total > 0  # 确认价目生效（不是 unknown）
    assert cost_tools.total == cost_base.total
    assert cost_tools.as_dict() == cost_base.as_dict()

    # 归一化路径同样不参与计价（tools_* 不能从任何口径渗进金额）
    norm_base = normalize_token_usage_dict(base)
    norm_tools = normalize_token_usage_dict(with_tools)
    assert calc_cost(norm_tools, quote, ts=0).as_dict() == calc_cost(norm_base, quote, ts=0).as_dict()
    assert norm_tools["total_tokens"] == norm_base["total_tokens"]

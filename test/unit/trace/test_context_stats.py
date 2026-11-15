import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from utils.agent_trace import context_stats


def _step(step_type, duration_ms, messages=None, extra=None, step_id=1, status="success",
          started_at="2026-10-16T00:00:00+00:00"):
    return {"id": step_id, "run_id": "r1", "event_run_id": "e1", "parent_run_id": None,
            "step_type": step_type, "step_name": step_type, "status": status,
            "duration_ms": duration_ms, "started_at": started_at,
            "finished_at": "2026-10-16T00:00:10+00:00",
            "extra": extra or "{}", "messages": messages or []}


def test_estimate_tokens_empty():
    assert context_stats.estimate_tokens("") == 0
    assert context_stats.estimate_tokens(None) == 0


def test_estimate_tokens_uses_chars_per_token():
    assert context_stats.estimate_tokens("a" * 16) == 10   # 锁住 CHARS_PER_TOKEN=1.6


def test_parse_extra_tolerates_bad_json():
    assert context_stats.parse_extra("{bad") == {}
    assert context_stats.parse_extra("{\"a\": 1}") == {"a": 1}
    assert context_stats.parse_extra({"a": 1}) == {"a": 1}


def test_parse_extra_rejects_non_dict_json():
    assert context_stats.parse_extra("[1,2]") == {}


def test_classify_message_six_buckets():
    assert context_stats.classify_message({"role": "system", "content": "x"}) == "system"
    assert context_stats.classify_message({"role": "tool", "content": "x"}) == "tool_results"
    assert context_stats.classify_message({"role": "ai", "content": "x"}) == "assistant"
    assert context_stats.classify_message({"role": "human", "content": "帮我看看茅台"}) == "user"


def test_classify_injected_by_marker():
    # ⚠️ 第 2 条锁住一个容易实现错的点：压缩摘要由
    # agents/react/context.py::_summary_to_message 产出 AIMessage（role="ai"），
    # 它是「被注入的上下文」而不是模型自己的回答，必须归 injected。
    assert context_stats.classify_message(
        {"role": "human", "content": "[用户持仓账本]\n贵州茅台(600519) 成本1680.00×200股"}) == "injected"
    assert context_stats.classify_message(
        {"role": "ai", "content": "【已压缩历史轮次摘要】前两轮"}) == "injected"


def test_classify_role_outranks_inject_marker():
    """判定优先级：role 分派先于注入标记（标记只在 human / ai 分支内生效）。

    变异验证：把注入标记检查提到 role 分派之前，本用例立刻红
    ——system / tool 里出现同名字符串会被误判成 injected。
    """
    assert context_stats.classify_message(
        {"role": "system", "content": "【已压缩历史轮次摘要 出现在 system 里"}) == "system"
    assert context_stats.classify_message(
        {"role": "tool", "content": "[用户持仓账本] 工具回显用户持仓"}) == "tool_results"


def test_classify_fallbacks():
    assert context_stats.classify_message({}) == "assistant"
    assert context_stats.classify_message({"role": "ai", "content": 12345}) == "assistant"


def test_inject_markers_match_real_producers():
    """标记是字符串耦合：INJECT_MARKERS 必须与真产出点一一对上。

    否则注入会被静默归到 user —— 本项目已经踩过一次：第一版 4 个标记全是凭空猜的，
    [持仓信息] / 【持久记忆】 / 【注入】 全仓无生产者。产出点改 header 时这里必须红。
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    producers = {
        "[市场状态]": "agents/base.py",
        "[相关记忆片段]": "memory/sdk.py",
        "[用户持仓账本]": "agents/base.py",
        "【已压缩历史轮次摘要": "agents/react/context.py",
    }
    for marker, rel in producers.items():
        assert marker in (root / rel).read_text(encoding="utf-8"), (rel, marker)
    # 双向锁：常量本身也受管（多一个 / 少一个 / 改错一个都要红）
    assert set(context_stats.INJECT_MARKERS) == set(producers)


def test_step_category_tokens_sum():
    msgs = [{"role": "system", "content": "a" * 160},
            {"role": "tool", "content": "b" * 320}]
    buckets = context_stats._step_category_tokens({"messages": msgs})
    assert buckets["system"] == 100   # 160 / 1.6
    assert buckets["tool_results"] == 200
    assert sum(buckets.values()) == 300


def test_step_category_tokens_splits_injected_from_user():
    """注入块拼在用户原话之后：用户部分必须留在 user，不能被整条吞进 injected。

    切点 = 最早的注入标记（agents/base.py::_enrich_user_input 是逐块追加的）。
    """
    head = "帮我看看茅台\n"
    injected = "[用户持仓账本]\n贵州茅台(600519) 成本1680.00×200股"
    buckets = context_stats._step_category_tokens({
        "messages": [{"role": "human", "content": head + injected}]})
    assert buckets["user"] == context_stats.estimate_tokens(head)
    assert buckets["injected"] == context_stats.estimate_tokens(injected)
    assert buckets["user"] > 0
    assert buckets["injected"] > 0
    # ⚠️ 这里刻意改用**显式期望值**，不再写
    #   buckets["user"] + buckets["injected"] == estimate_tokens(head + injected)
    # —— 那条只在 int() 截断恰好抵消时成立（只去掉 head 的换行就 25 != 26 而红），
    #    属于「靠巧合成立」的断言，锁不住任何真实语义。
    assert buckets["user"] == 4        # len("帮我看看茅台\n") == 7   -> int(7 / 1.6)
    assert buckets["injected"] == 22   # len(injected) == 36          -> int(36 / 1.6)


def test_split_injected_uses_earliest_marker_with_multiple_blocks():
    """多块注入（真实形态）必须按**最早**标记切分：injected 含全部注入块，user 只含用户侧。

    真实输入 = 「[背景信息] / [用户问题] 时间骨架 + 用户原话 + [市场状态] + [相关记忆片段] +
    [用户持仓账本]」，注入块由 agents/base.py::_enrich_user_input 逐块追加在用户原话之后。

    变异验证：把 _split_injected 的 `cut = min(cut, pos)` 改成 `cut = pos`（取**最后**
    一个标记）本用例立刻红 —— user 会把 [市场状态] / [相关记忆片段] 两块吞进去。
    ⚠️ 只有单标记用例时这个变异是**存活的**（任务 3 复审实测全绿），多块形态必须先锁住。
    """
    head = ("[背景信息]\n- 当前时间：2026-10-17 10:00 周五\n\n[用户问题]\n帮我看看茅台\n")
    blocks = ("[市场状态]\n正常开市\n"
              "[相关记忆片段]\n用户偏好白酒\n"
              "[用户持仓账本]\n账户：总资产约520000.00元")
    buckets = context_stats._step_category_tokens({
        "messages": [{"role": "human", "content": head + blocks}]})
    # user 只含用户侧（含无标记的 [背景信息] / [用户问题] 骨架，登记为已知下界）
    assert buckets["user"] == context_stats.estimate_tokens(head)
    # injected 必须含**全部**三块注入，而不是「从最后一个标记起」
    assert buckets["injected"] == context_stats.estimate_tokens(blocks)


def test_bucket_and_message_level_agree_without_markers():
    """无标记消息：桶级头部归属必须与消息级 classify_message 同口径（防两处 role 判定漂移）。

    有标记时两者**有意**不同（消息级答 injected、桶级拆开计数），见 _step_category_tokens。
    """
    for role, expected in (("system", "system"), ("tool", "tool_results"),
                           ("ai", "assistant"), ("human", "user"), (None, "assistant")):
        message = {"role": role, "content": "a" * 16}
        assert context_stats.classify_message(message) == expected
        buckets = context_stats._step_category_tokens({"messages": [message]})
        assert {k: v for k, v in buckets.items() if v} == {expected: 10}


def test_billed_and_current_are_separate(monkeypatch):
    # 两次调用：第一次 1000 in / 200 out，第二次 3000 in / 400 out
    steps = [
        _step("llm", 8000, extra="{\"token_usage\": {\"input_tokens\": 1000, \"output_tokens\": 200}}"),
        _step("llm", 9000, extra="{\"token_usage\": {\"input_tokens\": 3000, \"output_tokens\": 400}}"),
    ]
    fake = _FakeStore({"id": "r1"}, steps)
    snap = context_stats.build_run_context("r1", store=fake)
    # 计费口径 = 逐次求和
    assert snap["totals"]["billed_tokens"] == 4600
    assert snap["totals"]["llm_calls"] == 2
    # 当前上下文口径 = 最后一次调用
    assert snap["totals"]["current_input_tokens"] == 3000
    # 两者绝不能相等（相等说明口径被混用）
    assert snap["totals"]["billed_tokens"] != snap["totals"]["current_input_tokens"]


def test_split_output_identity():
    """口径铁律 2：思考 + 回答 == output_tokens，**逐 token 相等**。

    变异验证：把实现改成 `"answer": output`（不减思考）本用例立刻红 ——
    这是本任务的核心恒等式，必须被锁住。
    """
    assert context_stats.split_output({"output_tokens": 1000, "reasoning_tokens": 661}) == {
        "thinking": 661, "answer": 339}


def test_split_output_zero_thinking_keeps_full_answer():
    # 没采到思考 != 思考为 0 把回答也清零
    assert context_stats.split_output({"output_tokens": 500}) == {"thinking": 0, "answer": 500}


def test_split_output_clamps_bad_provider_data():
    # 脏数据（思考 > 输出）按输出封顶，回答归 0，绝不出现负数回答
    out = context_stats.split_output({"output_tokens": 100, "reasoning_tokens": 999})
    assert out == {"thinking": 100, "answer": 0}


def test_thinking_not_double_counted_into_billed_total():
    steps = [_step("llm", 1000, extra="{\"token_usage\": {\"input_tokens\": 10, \"output_tokens\": 100, \"total_tokens\": 110, \"reasoning_tokens\": 66}}")]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    # 计费总量必须是 provider 的 total_tokens，不是 total + 思考
    assert snap["totals"]["billed_tokens"] == 110
    assert snap["output_split"] == {"thinking": 66, "answer": 34}


def test_thinking_ratio_and_coverage_are_none_without_data():
    """思考占比 / 思考覆盖率：分母为 0 时必须 None 而不是 0（铁律 3 在 totals 上的落地）。

    变异验证：把实现里的 `else None` 改成 `else 0` 本用例立刻红 ——
    0% 会被 UI 读成「模型没有思考」，与「未采集」是两个口径。
    """
    steps = [_step("llm", 10, extra=json.dumps(
        {"token_usage": {"input_tokens": 10, "output_tokens": 100, "reasoning_tokens": 66}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["totals"]["thinking_ratio_of_output"] == 0.66
    assert snap["totals"]["calls_with_thinking"] == 1
    assert snap["totals"]["thinking_calls_coverage"] == 1.0
    # 没有真实 LLM 调用：两个比率都没有分母，必须是 None
    empty = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, []))
    assert empty["totals"]["thinking_ratio_of_output"] is None
    assert empty["totals"]["thinking_calls_coverage"] is None


def test_cache_hit_pseudo_step_not_counted():
    steps = [
        _step("llm", 1000, extra="{\"token_usage\": {\"input_tokens\": 3000, \"output_tokens\": 400, \"total_tokens\": 3400}}"),
        _step("llm", 0, extra="{\"cache_hit\": true, \"saved_tokens\": 1200, \"label\": \"缓存命中\"}"),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["totals"]["llm_calls"] == 1                 # 伪步骤不算调用
    assert snap["totals"]["current_input_tokens"] == 3000   # 不能被伪步骤清零
    assert snap["totals"]["billed_tokens"] == 3400


def test_llm_error_step_does_not_clear_current(monkeypatch):
    """出错结束的 llm 步骤（on_llm_error，无 token_usage）不得把 current_input 清零，
    且它进 requests[] 的那一行**不能把「未采集」装成 0 值**（铁律 3）。
    """
    from config import Config

    monkeypatch.setattr(Config, "CONTEXT_WINDOW_TOKENS", 10000)
    steps = [
        _step("llm", 1000, extra="{\"token_usage\": {\"input_tokens\": 3000, \"output_tokens\": 100}}"),
        _step("llm", 500, extra="{\"label\": \"调用失败\"}", status="error"),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["totals"]["current_input_tokens"] == 3000
    # 失败调用同样进 requests（tokens 全 0、cost None），必须靠 status 与成功调用区分
    failed = snap["requests"][1]
    assert failed["status"] == "error"
    # ⚠️ tools 是唯一例外：失败调用的 tools 桶给 **None**（未埋点），不是 0。
    #    tools_chars == 0 与「没埋点」同形，靠哨兵 tools_measured 区分 —— 折成 0 会被读成
    #    「这次调用实测没绑工具」（口径铁律 3）。其余五类来自 messages，本步为空故仍是 0。
    assert failed["tokens"] == {**{name: 0 for name in context_stats.CATEGORIES}, "tools": None}
    assert failed["cost"] is None
    # ⚠️ 命中率的分母是 input_tokens（本次未采集）-> None，绝不能给 0.0
    #    —— 0.0 会被读成「实测 0% 命中」，与「未采集」同值不同义（铁律 3）。
    #    同源对照：occupancy 的分母是**窗口上限**（配好的常量，确实存在），所以它给的是真实的
    #    0 / limit = 0.0（「这次调用占用了窗口的 0%」）；这一行的「未采集」信号由 status 承担。
    assert failed["cache_hit_ratio"] is None
    assert failed["occupancy"] == 0.0


def test_requests_and_composition_are_assembled():
    """requests[]（每次调用明细）与 composition（**最后一次**调用的六分类）必须由本模块装配。

    设计 §4.2 的字段对照表直接消费这两个键 —— 底座不装配，任务 10 写 UI 时会自己发明
    一套聚合，直接破坏「口径唯一」。
    """
    steps = [
        _step("llm", 1000,
              extra=json.dumps({
                  "label": "执行", "node": "unified_executor", "round_no": 1,
                  "phase": "exec", "cost": {"total": 0.0147, "billing": "priced"},
                  "token_usage": {"input_tokens": 3000, "output_tokens": 400,
                                  "total_tokens": 3400, "reasoning_tokens": 264}}),
              messages=[{"role": "system", "content": "s" * 160},
                        {"role": "tool", "content": "t" * 320}]),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    req = snap["requests"][0]
    assert req["label"] == "执行" and req["node"] == "unified_executor"
    assert req["round_no"] == 1 and req["phase"] == "exec" and req["kind"] == "main"
    assert req["tokens"]["system"] == 100 and req["tokens"]["tool_results"] == 200
    assert req["thinking_tokens"] == 264 and req["answer_tokens"] == 136
    assert req["cost"] == 0.0147
    # composition = 最后一次调用的六分类；tools 来自网关埋点，未埋点必须是 None（口径铁律 3）
    assert snap["composition"]["system"] == 100
    assert snap["composition"]["tool_results"] == 200
    assert snap["composition"]["tools"] is None
    assert snap["output_split"] == {"thinking": 264, "answer": 136}


def test_request_cost_is_none_when_unpriced():
    """未计价 != 0 元：extra.cost.total 缺失时必须 None，不能折成 0。

    变异验证：把实现改成 `(cost or {}).get("total") or 0` 本用例立刻红
    —— 0 会被读成「计价为 0」，与「未计价」是两个口径（AGENTS.md 成本三约束之三）。
    """
    steps = [_step("llm", 10, extra=json.dumps({"cost": {"billing": "free"}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["requests"][0]["cost"] is None


@pytest.mark.parametrize("fork_kind, expected", [
    ("llm_fork", "fork"),     # 真的复用了热前缀（agents/shared/llm_fork.py:38/124）
    ("standalone", "main"),   # 明确没 fork（agents/react/context.py:157 / report_builder.py:298）
    ("none", "main"),         # 没有前缀可复用（agents/shared/llm_fork.py:118）
    (None, "main"),           # 无该键 = 普通主调用
])
def test_request_kind_only_counts_real_fork(fork_kind, expected):
    """fork_kind 的三个取值**全是非空真值**，判据必须精确到 "llm_fork"。

    变异验证：把实现改回真值判断（if step_extra.get("fork_kind")）后 standalone / none
    两个负例立刻红 —— 它们会被误标成 fork，即「明确没复用前缀」被算成复用。
    ⚠️ 取值域取自 agents/shared/llm_fork.py::fork_metadata 的三个分支，勿用编造值。
    """
    extra = {"fork_kind": fork_kind} if fork_kind else {"label": "执行"}
    steps = [_step("llm", 10, extra=json.dumps(extra))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["requests"][0]["kind"] == expected


def test_request_has_exactly_the_contract_fields():
    """requests[] 的字段集是对外契约（任务 10 的字段对照表按它渲染），多一个少一个都要红。

    status 不能少：on_llm_error 的调用也进这张表，没有它就分不清「失败」与「成功但 0 token」。
    变异验证：删掉实现里的 "status" 行本用例立刻红。
    """
    steps = [_step("llm", 10, step_id=7,
                   extra=json.dumps({"token_usage": {"input_tokens": 1, "output_tokens": 2}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    req = snap["requests"][0]
    assert set(req) == {
        "step_id", "event_run_id", "label", "node", "round_no", "phase", "kind",
        "tokens", "input_tokens", "output_tokens", "thinking_tokens", "answer_tokens",
        "cached_tokens", "cache_hit_ratio", "occupancy", "cost", "status", "duration_ms",
    }
    # step_id / event_run_id 是「点击跳到该步」的锚点，必须透传而不是写死
    assert req["step_id"] == 7
    assert req["event_run_id"] == "e1"
    assert req["input_tokens"] == 1 and req["output_tokens"] == 2
    assert req["cached_tokens"] == 0 and req["duration_ms"] == 10
    assert req["status"] == "success"


def test_composition_defaults_when_no_requests():
    """没有任何真实 LLM 调用时：requests 为空表，composition 六分类全 0 而 tools 仍为 None。"""
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, []))
    assert snap["requests"] == []
    assert snap["composition"] == {"system": 0, "tools": None, "user": 0,
                                   "injected": 0, "assistant": 0, "tool_results": 0}


def test_composition_takes_last_call_not_first():
    """composition 是**最后一次**调用（current 口径）的构成，不是第一次、也不是全 run 求和。

    变异验证：把实现改成 requests[0] 本用例立刻红 —— 两次调用的 messages 不同，
    「最后一次」这个语义必须有测试锁住（单次调用的用例分辨不出来）。
    """
    steps = [
        _step("llm", 1000, extra=json.dumps({"token_usage": {"input_tokens": 100, "output_tokens": 10}}),
              messages=[{"role": "system", "content": "a" * 160}]),
        _step("llm", 1000, extra=json.dumps({"token_usage": {"input_tokens": 300, "output_tokens": 20}}),
              messages=[{"role": "system", "content": "b" * 320}]),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert len(snap["requests"]) == 2
    assert snap["requests"][0]["tokens"]["system"] == 100
    assert snap["composition"]["system"] == 200          # 320 / 1.6，来自**最后**一次调用


def test_request_tools_uses_measured_chars():
    """tools_measured 为真 -> 按字符数换算；为假 -> None（未采集，绝不填 0）。"""
    steps = [
        _step("llm", 10, extra=json.dumps({"token_usage": {
            "input_tokens": 528, "output_tokens": 2, "total_tokens": 530,
            "tools_count": 2, "tools_chars": 620, "tools_measured": True}})),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    # 620 字符 / 1.6 = 387.5 -> 387。**不能**写成 estimate_tokens(620)：它量的是 str(620)
    # 的长度（3 字符 -> 1 token），语义完全不对。
    assert snap["requests"][0]["tokens"]["tools"] == 387
    assert snap["composition"]["tools"] == 387


def test_tools_tokens_matches_estimate_tokens_conversion():
    """换算口径与 estimate_tokens 同系数（CHARS_PER_TOKEN），不是第二套算法。"""
    usage = {"tools_measured": True, "tools_chars": 620}
    assert context_stats._tools_tokens(usage) == context_stats.estimate_tokens("x" * 620)
    assert context_stats._tools_tokens({"tools_measured": True, "tools_chars": 0}) == 0
    assert context_stats._tools_tokens({"tools_measured": False, "tools_chars": 999}) is None


def test_request_tools_is_none_when_not_measured():
    """未埋点（流式 / response_format / 无该字段）-> None，不能显示成 0 个 token。"""
    steps = [_step("llm", 10, extra=json.dumps({"token_usage": {
        "input_tokens": 100, "output_tokens": 5, "total_tokens": 105}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["requests"][0]["tokens"]["tools"] is None
    assert snap["composition"]["tools"] is None


def test_request_tools_is_zero_when_measured_without_tools():
    """真绑了 0 个工具 -> 0（是实测值，不是「未采集」）。这是加哨兵的全部意义。"""
    steps = [_step("llm", 10, extra=json.dumps({"token_usage": {
        "input_tokens": 30, "output_tokens": 5, "total_tokens": 35,
        "tools_count": 0, "tools_chars": 0, "tools_measured": True}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["requests"][0]["tokens"]["tools"] == 0
    assert snap["composition"]["tools"] == 0


def test_request_tools_tolerates_dirty_tools_chars():
    """脏数据（tools_chars 非数字）不得抛异常：按 0 处理，但仍算「已采集」。

    extra 是自由 JSON，DB 里可能是字符串/None；本模块全篇「脏数据不抛」同规格
    （parse_extra / _role_of / window_limit）。哨兵为真时给实测 0，不退回 None。
    """
    steps = [_step("llm", 10, extra=json.dumps({"token_usage": {
        "input_tokens": 10, "output_tokens": 1, "tools_chars": "n/a",
        "tools_measured": True}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["requests"][0]["tokens"]["tools"] == 0


def test_request_tools_is_none_for_dirty_truthy_sentinel():
    """哨兵判据必须是 `is True`：字符串 "false"/"0" 一律视为**未采集**（fail-safe）。

    extra 是自由 JSON；若写成 bool(x or False)，"false" 会被读成「已采集」，而它
    **必然**伴随 tools_chars 缺省 → 消费侧显示假 0 —— 正是哨兵要防的方向。
    脏数据只允许落到 None，不允许落到 0。
    """
    for dirty in ("false", "0", 0, None):
        steps = [_step("llm", 10, extra=json.dumps({"token_usage": {
            "input_tokens": 10, "output_tokens": 1, "tools_chars": 620,
            "tools_measured": dirty}}))]
        snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
        assert snap["requests"][0]["tokens"]["tools"] is None, dirty


def test_request_tools_never_negative():
    """脏负数不得画出负扇区：封顶 0（同文件 split_output 的负数封顶先例）。"""
    steps = [_step("llm", 10, extra=json.dumps({"token_usage": {
        "input_tokens": 10, "output_tokens": 1, "tools_chars": -620,
        "tools_measured": True}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["requests"][0]["tokens"]["tools"] == 0


def test_composition_tools_absent_requests_stays_none():
    """空 requests 时 composition.tools 仍为 None（不能因为 requests 为空就编个 0）。"""
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, []))
    assert snap["composition"]["tools"] is None


def test_window_limit_tolerates_non_str_model():
    """非 str 的 model_name（来自 steps.extra 脏数据）不得抛异常，且与默认路径等价。

    不 pin 具体数值：窗口上限是可配置项，.env 改过就会本地红。
    """
    base, base_source = context_stats.window_limit("")
    assert context_stats.window_limit(123)[0] == base
    assert context_stats.window_limit(123)[1] == base_source
    assert context_stats.window_limit(None)[1] == base_source


def test_window_limit_hits_config_by_model(monkeypatch):
    """命中 CONTEXT_WINDOW_BY_MODEL 时返回该模型的映射值与 source=config_by_model。

    ⚠️ 只在进程内 monkeypatch 类属性：.env 被 gitignore 且每次启动被
    generate_env_from_template 回填，改它既看不见也留不住（reload 用 override=True）。
    """
    from config import Config

    monkeypatch.setattr(Config, "CONTEXT_WINDOW_BY_MODEL",
                        json.dumps({"unit-test-model": 32768}))
    assert context_stats.window_limit("unit-test-model") == (32768, "config_by_model")


def test_model_name_from_step_extra_with_fallback(monkeypatch):
    """模型名取自 steps.extra.model_name；后一条步骤没记则沿用前一条（回退逻辑）。

    窗口映射同样显式 monkeypatch，不依赖 .env 当前值。
    """
    from config import Config

    monkeypatch.setattr(Config, "CONTEXT_WINDOW_BY_MODEL",
                        json.dumps({"unit-test-model": 11111}))
    steps = [
        _step("llm", 1000, extra=json.dumps({
            "token_usage": {"input_tokens": 100, "output_tokens": 10},
            "model_name": "unit-test-model"})),
        _step("llm", 2000, extra=json.dumps({
            "token_usage": {"input_tokens": 300, "output_tokens": 20}})),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["run"]["model"] == "unit-test-model"      # 不能被后一条空值覆盖
    assert snap["window"] == {"limit": 11111, "source": "config_by_model"}


def test_build_run_context_returns_none_for_missing_run():
    """接口契约：run 不存在返回 None。任务 9 的 HTTP 层靠它给出 404 语义，必须锁住。"""
    assert context_stats.build_run_context("nope", store=_FakeStore(None, [])) is None


def test_timing_sigma_and_overlap():
    """Σ 口径：各类按 step_type 求和，因此可以超过墙钟；超出部分必须**显式**报成 overlap_ms。

    变异验证（护栏一）：把 overlap_ms 改成恒 0，本用例的 overlap_ms == 200 立刻红
    —— 这就是「不许把 Σ 超墙钟藏起来」的那道锁。
    """
    steps = [_step("llm", 9200), _step("tool", 1000)]
    timing = context_stats.build_timing(steps, wall_ms=10000)
    assert timing["llm_ms"] == 9200
    assert timing["tool_ms"] == 1000
    assert timing["wall_ms"] == 10000
    assert timing["sum_ratio"] == 1.02
    assert timing["overlap_ms"] == 200


def test_timing_never_uses_parent_tree():
    """耗时只按 step_type 求和：parent_run_id 是空还是缺失都不影响结果（不建父子树）。"""
    steps = [dict(_step("llm", 100), parent_run_id=None),
             dict(_step("tool", 50), parent_run_id="")]
    timing = context_stats.build_timing(steps, wall_ms=200)
    assert timing["llm_ms"] == 100 and timing["tool_ms"] == 50
    assert timing["sum_ratio"] == 0.75 and timing["overlap_ms"] == 0


def test_slowest_llm_calls_sorted_with_ms_per_1k():
    steps = [
        _step("llm", 6200, extra="{\"token_usage\": {\"input_tokens\": 47600}, \"label\": \"生成计划\", \"node\": \"planner\"}"),
        _step("llm", 7400, extra="{\"token_usage\": {\"input_tokens\": 3900}, \"label\": \"分类\", \"node\": \"classifier\"}"),
    ]
    rows = context_stats.slowest_llm_calls(steps, top=2)
    assert rows[0]["duration_ms"] == 7400
    assert rows[0]["label"] == "分类"
    assert rows[0]["ms_per_1k"] == pytest.approx(1897.4, abs=0.2)
    assert rows[1]["node"] == "planner"


def test_timing_ratio_only_counts_llm_tool_collector():
    """分子**只含 llm / tool / data_collector** 三类（设计 §4.6）。

    编排步（supervisor / parallel_executor / agent 等）与 llm、tool 步是区间嵌套关系，
    加进分子必然重复计数（实测某 run 平铺求和 = 墙钟 4.24 倍）；by_type 仍如实全量列出，
    供 UI 展示各类型构成。
    """
    steps = [_step("llm", 100), _step("tool", 50), _step("data_collector", 250),
             _step("supervisor", 4000)]
    timing = context_stats.build_timing(steps, wall_ms=1000)
    assert timing["by_type"]["supervisor"] == 4000.0
    assert timing["collector_ms"] == 250
    assert timing["sum_ratio"] == 0.4
    assert timing["overlap_ms"] == 0


def test_timing_gives_none_where_there_is_no_wall_clock():
    """墙钟缺失（runs.duration_ms 为空）时没有分母：比值与重叠都必须 None，不得折成 0。

    变异验证（护栏二）：把 sum_ratio 折成 min(1.0, ...)（clamp 到 1.0）后
    test_timing_sigma_and_overlap 的 sum_ratio == 1.02 立刻红 —— 「Σ 超墙钟」不许被压成
    「刚好占满」。把 overlap_ms 改回 max(0, total - wall) 时本用例立刻红 —— 没有墙钟时那
    等于凭空断言「全部耗时都是并发/重试挤出来的」。
    """
    timing = context_stats.build_timing([_step("llm", 100)], wall_ms=None)
    assert timing["wall_ms"] == 0
    assert timing["sum_ratio"] is None
    assert timing["overlap_ms"] is None


def test_snapshot_assembles_timing_and_slowest_llm():
    """快照必须自己装配 timing / slowest_llm（任务 10 的字段对照表直接吃它们）。

    墙钟 = runs.duration_ms（唯一分母）；slowest_llm 传的是 llm_steps，
    故 cache_hit 补记伪步骤（自述「无真实 LLM 调用」）不得混进「最慢调用」表。
    （伪步骤落库时 duration_ms 被显式置 0，见 core.record_cache_hit —— 但口径上仍必须排除它，
    否则「最慢调用」表会多出一行「花了 0 token / 0ms 的缓存命中」。）
    """
    steps = [
        _step("llm", 9200, extra=json.dumps({"token_usage": {"input_tokens": 47600},
                                             "label": "生成计划", "node": "planner"})),
        _step("tool", 1000),
        _step("llm", 0, extra=json.dumps({"cache_hit": True, "label": "缓存命中"})),
    ]
    snap = context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1", "duration_ms": 10000}, steps))
    assert snap["timing"]["wall_ms"] == 10000
    assert snap["timing"]["llm_ms"] == 9200 and snap["timing"]["tool_ms"] == 1000
    assert snap["timing"]["sum_ratio"] == pytest.approx(1.02)
    assert snap["timing"]["overlap_ms"] == pytest.approx(200)
    assert [r["label"] for r in snap["slowest_llm"]] == ["生成计划"]


def test_totals_has_exactly_the_contract_fields():
    """totals 的字段集是对外契约（任务 10 的字段对照表按它渲染），多一个少一个都要红。

    前 11 个是任务 4/4b 已装配的；cache_hit_ratio / cost / tool_calls / steps / human_inputs
    是本任务按设计 §4.2 补齐的（turns 属会话级，设计已标注未装配，不在这里自己造）。
    """
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, []))
    assert set(snap["totals"]) == {
        "billed_tokens", "input_tokens", "output_tokens", "cached_tokens",
        "cache_hit_ratio", "thinking_tokens", "answer_tokens", "thinking_ratio_of_output",
        "current_input_tokens", "llm_calls", "calls_with_thinking",
        "thinking_calls_coverage",
        "cost", "tool_calls", "steps", "human_inputs",
    }
    # 空 run：两个「没有分母」的比值是 None；三个计数是 0（真的没有，不是未采集）
    assert snap["totals"]["thinking_ratio_of_output"] is None
    assert snap["totals"]["thinking_calls_coverage"] is None
    assert snap["totals"]["tool_calls"] == 0
    assert snap["totals"]["steps"] == 0
    assert snap["totals"]["human_inputs"] == 0
    # 一次调用都没有 = 未计价（全为 None），不是「花了 0 元」
    assert snap["totals"]["cost"] is None
    # 没有 input_tokens 就没有缓存命中率的分母（铁律 3）
    assert snap["totals"]["cache_hit_ratio"] is None


def test_totals_cost_sums_priced_calls_and_is_none_when_all_unpriced():
    """totals.cost = Σ 各次 extra.cost.total；全为 None（未计价）时给 None，绝不折成 0。

    变异验证：把实现改成 "cost": cost_total（丢掉 cost_seen 判断）本用例立刻红 ——
    未计价会被读成「花了 0 元」，与「免费 / 计价为 0」混成一个口径（成本三约束之三）。
    """
    priced = [
        _step("llm", 10, extra=json.dumps({"cost": {"total": 0.0147, "billing": "priced"}})),
        _step("llm", 10, extra=json.dumps({"cost": {"total": 0.0141, "billing": "priced"}})),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, priced))
    assert snap["totals"]["cost"] == pytest.approx(0.0288)

    # 混合：未计价的那次不进金额，但不能把整条 run 判成「未计价」
    mixed = priced[:1] + [_step("llm", 10, extra=json.dumps({"cost": {"billing": "free"}}))]
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, mixed))["totals"]["cost"] == pytest.approx(0.0147)

    # 免费是**真实值 0.0**（计价为 0 元），不是「未计价」的 None
    free = [_step("llm", 10, extra=json.dumps({"cost": {"total": 0.0, "billing": "free"}}))]
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, free))["totals"]["cost"] == 0.0

    # 全为 None（未计价）：None，不是 0
    unpriced = [_step("llm", 10, extra=json.dumps({"cost": {"billing": "free"}})),
                _step("llm", 10, extra="{}")]
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, unpriced))["totals"]["cost"] is None


def test_totals_counts_steps_tool_calls_and_human_inputs():
    """三个计数字段各有口径（设计 §4.2）：

    totals.steps        = 步数（**数字**，与顶层 steps 列表同名不同义）
    totals.tool_calls   = step_type == "tool" 的步数（编排步不算工具调用）
    totals.human_inputs = 六分类 user 桶的**消息条数**（不是 token）
    """
    steps = [
        _step("llm", 10, extra=json.dumps({"token_usage": {"input_tokens": 1, "output_tokens": 1}}),
              messages=[{"role": "human", "content": "a" * 160},
                        {"role": "system", "content": "s" * 16}]),
        _step("tool", 10),
        _step("tool", 10),
        _step("supervisor", 10),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    totals = snap["totals"]
    assert totals["steps"] == 4 and isinstance(totals["steps"], int)
    assert totals["tool_calls"] == 2
    assert totals["human_inputs"] == 1
    # 条数而不是 token：这条 human 消息本身估算 100 token
    assert context_stats.estimate_tokens("a" * 160) == 100
    # ⚠️ 顶层同名键仍是**原始步骤列表**（同名不同义，勿混）
    assert isinstance(snap["steps"], list) and len(snap["steps"]) == 4


def test_human_inputs_counts_occurrences_not_deduped_turns():
    """human_inputs 是 Σ 口径：同一条用户消息出现在其后每一次调用的 prompt 里就再计一次
    （与 billed_tokens 同性质）。去重后的「用户轮次」属会话级 —— 设计已标注 turns 未装配，
    不要在这里假装是它。带注入标记的消息整条归 injected，不算用户输入。
    """
    same = [{"role": "human", "content": "帮我看看茅台"}]
    steps = [
        _step("llm", 10, messages=list(same),
              extra=json.dumps({"token_usage": {"input_tokens": 1}})),
        _step("llm", 10, messages=list(same),
              extra=json.dumps({"token_usage": {"input_tokens": 2}})),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["totals"]["llm_calls"] == 2
    assert snap["totals"]["human_inputs"] == 2

    injected = [{"role": "human", "content": "[用户持仓账本] 贵州茅台(600519) 成本1680.00×200股"},
                {"role": "ai", "content": "【已压缩历史轮次摘要】前两轮"}]
    steps2 = [_step("llm", 10, messages=injected)]
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, steps2))["totals"]["human_inputs"] == 0


def test_request_cache_hit_ratio_and_occupancy(monkeypatch):
    """requests[] 的两个字段（**与 totals 同公式，但含义不同**）：

    cache_hit_ratio = cached / input（**逐次**口径；无分母给 None）
    occupancy       = input_tokens / window.limit（窗口上限循环里拿不到，故循环后统一回填）

    ⚠️ totals.cache_hit_ratio 是 **token 加权**（Σcached / Σinput）、requests[] 是逐次，
    两者**不可互相平均** —— 共用的是公式，不是含义。
    """
    from config import Config

    monkeypatch.setattr(Config, "CONTEXT_WINDOW_BY_MODEL", "")
    monkeypatch.setattr(Config, "CONTEXT_WINDOW_TOKENS", 10000)
    steps = [_step("llm", 10, extra=json.dumps(
        {"token_usage": {"input_tokens": 2500, "output_tokens": 10, "cached_tokens": 1250}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["window"] == {"limit": 10000, "source": "config"}
    req = snap["requests"][0]
    assert req["cache_hit_ratio"] == 0.5     # _cache_hit_ratio: 1250 / 2500
    assert req["occupancy"] == 0.25          # 2500 / 10000

    # **成功且真没命中**（input 有值、cached 缺省 0）：这是实测 0% 命中，必须 0.0 而不是 None
    # —— 否则「把两处一起改成恒 None」那个变异是活的（会吞掉真实的 0%）。
    miss = [_step("llm", 10, extra=json.dumps(
        {"token_usage": {"input_tokens": 1000, "output_tokens": 5}}))]
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, miss))["requests"][0]["cache_hit_ratio"] == 0.0

    # **成功但 provider 未回 input_tokens**：这是审查实测的那一行，也是「靠 status 兜底」的失效点
    # （status == success，消费方分不出未采集）。只有命中率自己给 None，才不会发「0% 命中」的假信号。
    no_input = [_step("llm", 10, extra=json.dumps({"token_usage": {"output_tokens": 5}}))]
    req_ni = context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, no_input))["requests"][0]
    assert req_ni["status"] == "success"
    assert req_ni["cache_hit_ratio"] is None


def test_request_occupancy_is_none_when_window_limit_is_zero(monkeypatch):
    """limit 为 0 时占用率给 None 而不是 0（铁律 3：没有分母就没有占用率）。"""
    from config import Config

    monkeypatch.setattr(Config, "CONTEXT_WINDOW_BY_MODEL",
                        json.dumps({"unit-zero-window": 0}))
    steps = [_step("llm", 10, extra=json.dumps(
        {"token_usage": {"input_tokens": 100, "output_tokens": 1},
         "model_name": "unit-zero-window"}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["window"] == {"limit": 0, "source": "config_by_model"}
    assert snap["requests"][0]["occupancy"] is None


def test_totals_cache_hit_ratio_has_guard():
    """totals.cache_hit_ratio = cached_tokens / input_tokens；分母为 0 时必须 None。

    理由不只是对齐设计 §4.2：任务 10 的 KPI 表若自己手工做这个除法，input_tokens 为 0 时会
    得到 NaN / inf（前端拿到它渲染成什么是不可控的）—— 由底座给出**带守卫**的字段更安全。
    变异验证：去掉 `if input_tokens else None` 守卫（直接除）本用例立刻红（除零）。
    """
    steps = [_step("llm", 10, extra=json.dumps(
        {"token_usage": {"input_tokens": 2500, "output_tokens": 10, "cached_tokens": 1250}}))]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    assert snap["totals"]["cache_hit_ratio"] == 0.5

    # 有 llm 步骤但 provider 没回 input_tokens（全 0）：没有分母 -> None，不是 0
    zero = [_step("llm", 10, extra=json.dumps({"token_usage": {"output_tokens": 5}}))]
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, zero))["totals"]["cache_hit_ratio"] is None
    # 一次调用都没有：同样 None（不得除零）
    assert context_stats.build_run_context(
        "r1", store=_FakeStore({"id": "r1"}, []))["totals"]["cache_hit_ratio"] is None


def test_requests_sum_matches_totals():
    """两处独立算的字段必须始终相等：漂移要变成红灯，而不是静默给错数。

    requests[] 的 token 值在 _build_request 里取，totals 在累加循环里取 —— 同一份 usage 走
    两条路径。语料含两次成功调用（其一 cached 缺省 0）、一次无 usage 的失败调用、一次
    cache_hit 伪步骤与一个 tool 步。
    ⚠️ 只锁**真不变量**：这三项两边都是「同一份逐次值相加」，任一侧漏加/少加都要红。
    thinking / answer **不进这条**：split_output 对脏数据（思考 > 输出）各自封顶，
    「逐次封顶之和」与「汇总后一次封顶」在脏数据下**有意**不等（见 split_output 文档）。
    """
    steps = [
        _step("llm", 100, step_id=1, extra=json.dumps({
            "token_usage": {"input_tokens": 3000, "output_tokens": 400,
                            "total_tokens": 3400, "cached_tokens": 1500},
            "cost": {"total": 0.0147, "billing": "priced"}})),
        _step("llm", 200, step_id=2, extra=json.dumps({
            "token_usage": {"input_tokens": 1000, "output_tokens": 50},
            "cost": {"total": 0.0021, "billing": "priced"}})),
        _step("llm", 300, step_id=3, extra=json.dumps({"label": "调用失败"}), status="error"),
        _step("llm", 0, step_id=4, extra=json.dumps({"cache_hit": True, "label": "缓存命中"})),
        _step("tool", 500, step_id=5),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    requests, totals = snap["requests"], snap["totals"]
    assert len(requests) == 3                      # cache_hit 伪步骤不算真实调用
    for key in ("input_tokens", "output_tokens", "cached_tokens"):
        assert sum(r[key] for r in requests) == totals[key], key
    assert totals["input_tokens"] == 4000          # 3000 + 1000 + 0（失败调用没有用量）
    assert sum(r["cost"] or 0 for r in requests) == pytest.approx(totals["cost"] or 0)
    assert totals["cost"] == pytest.approx(0.0168)


def test_timing_excludes_cache_hit_pseudo_step():
    """build_timing 必须用**与 llm_steps 同款判据**排除 cache_hit 补记伪步骤。

    ⚠️ 不能依赖产出端「伪步骤 duration_ms=0」这个约定：core.record_cache_hit 目前确实写 0，
    但那是它的实现细节，不是本模块的口径（实测把伪步骤写成 999ms，没有本判据时 llm_ms 会被
    凭空抬高 999ms）。by_type 同样不含它 —— 否则 UI 的类型构成会凭空多出一块。
    变异验证：去掉 build_timing 里的该过滤，本用例立刻红。
    """
    steps = [_step("llm", 9200, extra=json.dumps({"token_usage": {"input_tokens": 47600}})),
             _step("llm", 999, extra=json.dumps({"cache_hit": True, "label": "缓存命中"}))]
    timing = context_stats.build_timing(steps, wall_ms=10000)
    assert timing["llm_ms"] == 9200
    assert timing["by_type"]["llm"] == 9200
    assert timing["sum_ratio"] == 0.92
    assert timing["overlap_ms"] == 0


class _FakeStore:
    def __init__(self, run, steps):
        self._run, self._steps = run, steps

    def get_run(self, run_id):
        return self._run

    def get_steps_with_messages(self, run_id):
        return self._steps



def test_events_from_compress_skill_fork_and_injection():
    """上下文事件（压缩 / 技能激活 / fork / 注入）—— 只报真实发生的事。

    ⚠️ 三条判据都是本任务修正过的点，各自都有负例守着：
      1) compress：compress_method == "none" 表示这一步**没真压缩**（产出点
         agents/react/tool_node_base.py:225 把空 compress_stages 写成 "none"），不得算事件。
      2) fork：fork_kind 的三取值（llm_fork / standalone / none，agents/shared/llm_fork.py:118-128）
         **全是非空真值**，判据必须精确到 "llm_fork"。
      3) inject：复用 classify_message 的「role 高于标记」口径（任务 3 已确立），
         tool 回显里出现同一标记串不得算注入。
    """
    steps = [
        _step("tool", 10, extra="{\"label\": \"mx_data_query\", \"compress\": {\"raw_len\": 1028, \"final_len\": 792, \"compress_method\": \"trim\"}}"),
        _step("tool", 10, extra="{\"label\": \"mx_search_news\", \"compress\": {\"raw_len\": 2955, \"final_len\": 2955, \"compress_method\": \"none\"}}"),
        _step("agent", 10, extra="{\"active_skill\": \"stock_analysis\", \"label\": \"执行\"}"),
        # ⚠️ fork_kind 的真实取值域是 llm_fork / standalone / none（agents/shared/llm_fork.py:118-128），
        #    三个都是非空真值 —— 不要写编造的取值（如 "report"），那会把错误行为锁成绿色。
        _step("llm", 10, extra="{\"fork_kind\": \"llm_fork\", \"label\": \"报告生成\"}"),
        # 注入标记用真产出点 [用户持仓账本]（agents/base.py:95 / server/routes/portfolio.py:200）
        _step("llm", 10, messages=[{"role": "human", "content": "[用户持仓账本] 600519"}]),
        # 工具回显里出现同一标记串：按「role 高于标记」**不得**算成注入事件
        _step("llm", 10, messages=[{"role": "tool", "content": "[用户持仓账本] 600519"}]),
    ]
    events = context_stats.build_events(steps)
    kinds = [e["kind"] for e in events]
    # 6 个步骤只产出 4 件真事，且按步序排列（第 6 步的 tool 回显不算）
    assert kinds == ["compress", "skill", "fork", "inject"]
    # compress_method == none 不算事件（没真压缩）
    assert sum(1 for e in events if e["kind"] == "compress") == 1
    # 只有 human 那条算注入（tool 回显不算）
    assert sum(1 for e in events if e["kind"] == "inject") == 1
    compress, skill, fork, inject = events
    assert compress["detail"]["compress_method"] == "trim"     # trim 是真实压缩阶段名
    # 差值 = final - raw（负数 = 压缩掉了内容；产出端两个长度恒为 int，见 tool_node_base.py:222-226）
    assert compress["delta_chars"] == 792 - 1028
    # detail.event = 子类型（真实产出总带 kind；本步是手工造的更窄 payload，故兜底为 None）
    assert skill["detail"] == {"skill": "stock_analysis", "event": None}
    assert fork["detail"] == {"fork_kind": "llm_fork", "prefix_msgs": None}
    assert inject["detail"] == {"messages": 1}
    # fork 事件只对 llm_fork 产出；standalone / none 是「没复用前缀」，不算 fork
    for fk in ("standalone", "none"):
        side = [_step("llm", 10, extra="{\"fork_kind\": \"%s\"}" % fk)]
        assert [e["kind"] for e in context_stats.build_events(side)] == []


def test_events_never_raise_on_bad_extra():
    """坏 extra 一律不抛（本模块全篇「脏数据不抛」的立场）：坏 JSON / 非 dict / 非 str /
    非数字长度各一例（extra 是自由 JSON，什么脏值都可能落进来）。

    变异验证：① 去掉 compress_method 的 str() 兜底（直接 .lower()）-> AttributeError；
    ② 去掉 raw_len / final_len 的 try 兜底 -> ValueError / TypeError；
    ③ 把兜底值 None 改成 0 -> 断言 delta_chars is None 立刻红（证明没编造 0）。
    """
    # 1) 坏 JSON：parse_extra 返回 {}（不抛）
    assert context_stats.build_events([_step("tool", 1, extra="{bad json")]) == []
    # 2) compress 不是 dict（脏数据 / 未来换形态）：跳过而不是抛，也不凭空产出事件
    assert context_stats.build_events([_step("tool", 1, extra='{"compress": true}')]) == []
    assert context_stats.build_events([_step("tool", 1, extra='{"compress": [1, 2]}')]) == []
    # 3) compress_method 不是 str：str() 兜底后按字面量比（非 "none" -> 仍是真压缩，出事件）
    for value in (123, True, ["trim"], {"a": 1}):
        step = _step("tool", 1, extra=json.dumps({"compress": {
            "raw_len": 10, "final_len": 4, "compress_method": value}}))
        assert [e["kind"] for e in context_stats.build_events([step])] == ["compress"]
    # 4) raw_len / final_len 解析不出 int：事件照报，但 delta_chars 给 None —— 不编造 0
    #    （铁律 3：缺失 != 0）；null（缺失）与 "abc" / [1] / {"a": 1}（坏值）走同一条路
    for field in ("raw_len", "final_len"):
        for bad in ("abc", [1], {"a": 1}, None):
            payload = {"raw_len": 10, "final_len": 4, "compress_method": "trim"}
            payload[field] = bad
            step = _step("tool", 1, extra=json.dumps({"compress": payload}))
            events = context_stats.build_events([step])
            assert [e["kind"] for e in events] == ["compress"], (field, bad)
            assert events[0]["delta_chars"] is None, (field, bad)


def test_events_fork_requires_llm_fork_kind():
    """fork 负例：fork_kind 是「三取值全是非空真值」的枚举，真值判断会凭空报 fork。

    变异验证：把 build_events 里的判据改回真值判断（if extra.get("fork_kind") 恒真），
    本用例立刻红 —— standalone / none（明确没复用热前缀）会各多出一个 fork 事件。
    语义与 requests[].kind（_build_request）及 server/quality_service.py:496 一致：
    fork 事件 = 该次副调用**真的复用了热前缀**。
    """
    for fk in ("standalone", "none"):
        assert context_stats.build_events(
            [_step("llm", 1, extra=json.dumps({"fork_kind": fk}))]) == []
    # 真 fork 才产出事件，且带上真实的 fork_prefix_msgs（agents/shared/llm_fork.py:126）
    events = context_stats.build_events([_step(
        "llm", 1, extra=json.dumps({"fork_kind": "llm_fork", "fork_prefix_msgs": 12}))])
    assert [e["kind"] for e in events] == ["fork"]
    assert events[0]["detail"] == {"fork_kind": "llm_fork", "prefix_msgs": 12}


def test_events_inject_reuses_classify_message():
    """注入负例：判定必须复用 classify_message（role 高于标记 + 非 str 兜底）。

    变异验证：把注入检测改回内联 marker in content（绕过 classify_message）：
      ① tool / system 里出现同名字符串会各多出一个 inject 事件（本用例头两条红）；
      ② 多模态块列表（content 是 list，marker in [dict] 恒 False）会静默漏判（第三条红）。
    """
    for role in ("tool", "system"):
        echo = [_step("llm", 1, messages=[{"role": role, "content": "[用户持仓账本] 600519"}])]
        assert context_stats.build_events(echo) == []
    # content 非 str（多模态块列表）：classify_message 的 str() 兜底仍能判出注入
    blocks = [_step("llm", 1, messages=[{"role": "human", "content": [
        {"type": "text", "text": "[用户持仓账本] 600519"}]}])]
    assert [e["kind"] for e in context_stats.build_events(blocks)] == ["inject"]
    # 注入只在 llm 步上查：非 llm 步带 messages 不算（没有 prompt 就没有「注入」）
    assert context_stats.build_events([_step("agent", 1, messages=[
        {"role": "human", "content": "[用户持仓账本] 600519"}])]) == []


def test_events_ignore_odd_shapes_and_use_real_skill_step_type():
    """空 / 缺字段不抛、也不凭空产出事件；技能事件用**真实产出形态**（step_type='skill'）。

    ⚠️ active_skill 的真产出点是 recorder.record_skill_event（utils/agent_trace/core.py:356
    以 step_type='skill' 落库；agents/react/nodes.py:573 与 plan/pdor 的 executor 都走它），
    不是 step_type='agent' —— 判据不得绑死在错误的 step_type 上。
    ⚠️ base 的 step_id / at 是**透传**的（不是写死 1 / 固定时间）：这里刻意给不同的值，
    否则「透传」与「写死」在断言上不可区分。
    """
    assert context_stats.build_events([]) == []
    assert context_stats.build_events([{"step_type": "tool"}]) == []   # 缺 id/extra/messages
    # active_skill 为空串 = 没激活（agents/react/nodes.py:527 无候选技能时就是 ""），不算事件
    assert context_stats.build_events(
        [_step("skill", 1, extra='{"active_skill": "", "label": "技能选择"}')]) == []
    # 真产出形态（record_skill_event 落库的 extra：active_skill / kind / label 同源）+ base 透传
    events = context_stats.build_events([_step(
        "skill", 1, step_id=77, started_at="2026-10-16T09:31:07+00:00",
        extra='{"active_skill": "stock_analysis", "kind": "select", "label": "技能选择"}')])
    assert events == [{"step_id": 77, "label": "技能选择",
                       "at": "2026-10-16T09:31:07+00:00",
                       "kind": "skill",
                       "detail": {"skill": "stock_analysis", "event": "select"}}]
    # reselect：主技能**没变**、只换了工具批（agents/react/skill_nodes.py:137）——
    # 它同样算技能变更，不得因为「技能名没变」被过滤（真数据里 5 条 skill 事件有 3 条是它）
    reselect = context_stats.build_events([_step(
        "skill", 1, extra='{"active_skill": "stock_analysis", "from": "stock_analysis", '
                          '"to": "stock_analysis", "kind": "reselect"}')])
    assert [e["detail"]["event"] for e in reselect] == ["reselect"]


def test_snapshot_assembles_events():
    """快照必须把事件接进来（任务 9 的 GET /api/runs/<id>/context 依赖 snap["events"]）。

    ⚠️ 接线必须传**全量 steps**，不是 llm_steps：压缩事件落在 tool 步上、技能事件落在
    skill 步上（真产出点 utils/agent_trace/core.py:356 以 step_type='skill' 落库），
    只有注入事件才看 llm 步。用 llm_steps 会让前两类事件全部丢失。
    变异验证：把 build_events(steps) 改成 build_events(llm_steps)，本用例立刻红。
    """
    steps = [
        _step("tool", 10, extra=json.dumps({"label": "mx_data_query", "compress": {
            "raw_len": 1028, "final_len": 792, "compress_method": "trim"}})),
        _step("skill", 10, extra=json.dumps({"active_skill": "stock_analysis",
                                             "kind": "activate",
                                             "label": "技能激活"})),
        _step("llm", 10, extra=json.dumps({"fork_kind": "llm_fork", "label": "报告生成"})),
        _step("llm", 10, messages=[{"role": "human", "content": "[用户持仓账本] 600519"}]),
    ]
    snap = context_stats.build_run_context("r1", store=_FakeStore({"id": "r1"}, steps))
    # 四类事件都能从快照拿到；label 沿 base 透传（最后一条 llm 步没有 extra.label -> step_name）
    assert [e["kind"] for e in snap["events"]] == ["compress", "skill", "fork", "inject"]
    assert [e["label"] for e in snap["events"]] == ["mx_data_query", "技能激活",
                                                    "报告生成", "llm"]


def test_events_one_step_can_emit_multiple_kinds():
    """同一步可以同时产出多类事件 —— **四判据互不排斥**（不是 if/elif 链）。

    真实形态：fork 与 inject 落在同一条 llm 步上（step 2788）；单个 llm 步的多条注入合并成
    **一条**事件（detail.messages 计数，真实 step 2414 是 2）。
    合成形态：同一步**同时**给 active_skill + fork_kind。当前产出端不会产生这种步
    （active_skill 只由 record_skill_event 写进新建的 skill 步；fork_kind 只经
    core._PASSTHROUGH_EXTRA_KEYS 白名单透传，而该白名单**不含** active_skill），
    但只要有人把 active_skill 加进那个白名单，两条链路就会落到同一步上 —— 届时必须产出
    [skill, fork] 两条。
    变异验证：① inject 判据改成挂在 fork 判据之后的 elif（假设「fork 过的步不再报 inject」）
    -> step 2788 的 inject 消失；② fork 判据改成挂在 skill 判据之后的 elif（假设四类互斥）
    -> 合成步的 fork 消失。两条都让本用例立刻红。
    """
    steps = [
        # 同一 llm 步：既复用了热前缀，prompt 里又带着注入（真实 step 2788）
        _step("llm", 10, step_id=2788, started_at="2026-10-16T09:30:00+00:00",
              extra=json.dumps({"fork_kind": "llm_fork", "fork_prefix_msgs": 21,
                                "label": "报告生成"}),
              messages=[{"role": "human", "content": "[用户持仓账本] 600519"}]),
        # 同一步 prompt 里两条注入（真实 step 2414）
        _step("llm", 10, step_id=2414,
              messages=[{"role": "human", "content": "[用户持仓账本] 600519"},
                        {"role": "ai", "content": "【已压缩历史轮次摘要】前两轮"}]),
        # 合成步：同一步既有技能变更又有 fork（钉住「四判据互不排斥」，见 docstring）
        _step("skill", 1, step_id=3001, started_at="2026-10-16T10:00:00+00:00",
              extra=json.dumps({"active_skill": "stock_analysis", "kind": "switch",
                                "fork_kind": "llm_fork", "fork_prefix_msgs": 9})),
    ]
    events = context_stats.build_events(steps)
    assert [e["kind"] for e in events] == ["fork", "inject", "inject", "skill", "fork"]
    # base 逐条透传：同一步的多条事件共享步号 / 时间，不同步不串味
    assert [(e["step_id"], e["label"]) for e in events] == [
        (2788, "报告生成"), (2788, "报告生成"), (2414, "llm"),
        (3001, "skill"), (3001, "skill")]
    assert [e["at"] for e in events] == ["2026-10-16T09:30:00+00:00",
                                         "2026-10-16T09:30:00+00:00",
                                         "2026-10-16T00:00:00+00:00",
                                         "2026-10-16T10:00:00+00:00",
                                         "2026-10-16T10:00:00+00:00"]
    assert events[0]["detail"] == {"fork_kind": "llm_fork", "prefix_msgs": 21}
    assert events[1]["detail"] == {"messages": 1}
    assert events[2]["detail"] == {"messages": 2}   # 一条步里两条注入合并成一条事件
    # 合成步的两条事件：判定顺序是 skill -> fork（同一份 extra 两类都命中）
    assert events[3]["detail"] == {"skill": "stock_analysis", "event": "switch"}
    assert events[4]["detail"] == {"fork_kind": "llm_fork", "prefix_msgs": 9}


# ── 跨会话聚合（任务 7）：总览大屏「上下文洞察」块的唯一数据源 ────────────────────
#
# 三处都是「口径铁律」的落点，改动前必读：
#   1) answer = output - reasoning（铁律 2：输出 = 思考 + 回答，思考只能作内部拆分）；
#   2) 空区间 / 全未计价一律 None，绝不显示成 0（铁律 3）；
#   3) 但「免费」（billing=free）是**已计价为 0**，必须给 0.0 —— 与「未计价」严格区分。
#
# ⚠️ 时间一律用 _iso_ago 相对构造，不写死日期：runs 是按「now - days」窗口裁剪的，
#    硬编码 2026-10-16 在 7 天之后就会自己滑出窗口变红（不是代码错了，是测试错了）。


def _iso_ago(**kwargs) -> str:
    """now - kwargs 的 UTC isoformat；与 runs.created_at 同格式（产出点 models._now）。"""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _run(run_id="r1", agent_name="react_stock", status="success",
         duration_ms=100, created_at=None):
    return {"id": run_id, "agent_name": agent_name, "status": status,
            "duration_ms": duration_ms,
            "created_at": _iso_ago(minutes=5) if created_at is None else created_at}


def _empty_agg(**overrides):
    """空区间的真实聚合形态（键名照 store.aggregate_llm_usage:590-606）。"""
    agg = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
           "reasoning_tokens": 0, "cached_tokens": 0,
           "cost_total": 0.0, "cost_unpriced_calls": 0, "cost_free_calls": 0,
           "cost_missing_calls": 0,
           "cache_hit_ratio": 0.0, "daily": [], "cost_daily": [], "by_model": [],
           "llm_calls": 0,
           # 按次计费服务（2026-10-18 加，见 store.aggregate_llm_usage 返回值）
           "service_cost": 0.0, "service_cost_daily": [], "service_calls": 0,
           "service_unpriced_calls": 0, "service_by_name": {}}
    agg.update(overrides)
    return agg


class _FakeInsightStore:
    """只实现 build_context_insight 依赖的两个方法，并记录调用参数。

    ⚠️ list_runs 的签名故意**只有 limit**：窗口口径统一在 Python 侧过滤、不把 since 传下去
    （真实 store.list_runs 支持并下推 since，但只留一个出口）。若实现改成
    list_runs(since=...)，这里直接 TypeError —— 所有 insight 用例一起红（钉住该口径）。
    """

    def __init__(self, agg, runs):
        self._agg, self._runs = agg, runs
        self.calls = []

    def aggregate_llm_usage(self, since="", days=14):
        self.calls.append(("aggregate_llm_usage", {"since": since, "days": days}))
        return self._agg

    def list_runs(self, limit=100):
        self.calls.append(("list_runs", {"limit": limit}))
        return self._runs


def test_build_context_insight_shape():
    """跨会话聚合的形状 + 输出拆分口径（answer = output - reasoning）。"""
    fake = _FakeInsightStore(
        agg=_empty_agg(total_tokens=1000, input_tokens=600, output_tokens=400,
                       reasoning_tokens=264, cost_total=1.5, cache_hit_ratio=0.9,
                       daily=[1, 2, 3], cost_daily=[0.1, 0.2, 0.3], llm_calls=5),
        runs=[_run("r1", "react_stock", "success", 100)])
    insight = context_stats.build_context_insight(days=7, store=fake)
    assert insight["kpi"]["billed_tokens"] == 1000
    assert insight["kpi"]["reasoning_tokens"] == 264
    assert insight["kpi"]["answer_tokens"] == 136      # 400 - 264
    assert insight["range"]["days"] == 7
    assert len(insight["runs"]) == 1
    assert insight["kpi"]["runs"] == 1
    assert insight["kpi"]["cache_hit_ratio"] == 0.9
    assert insight["kpi"]["cost"] == 1.5
    # 三段日线 / 按模型原样透传（大屏直接画图，不做二次加工）
    assert insight["daily_tokens"] == [1, 2, 3]
    assert insight["daily_cost"] == [0.1, 0.2, 0.3]
    assert insight["by_model"] == []


def test_insight_marks_missing_aggregates_as_none():
    """空区间 / 全未计价：不得把「未采集」显示成 0（铁律 3）。

    ⚠️ store.aggregate_llm_usage 对空区间返回 cache_hit_ratio=0.0、cost_total=0.0，
    且该行为被 test_aggregate_llm_usage.py 锁成 0.0（前人刻意决定，本计划不改它）——
    所以**必须在本函数兜住**，否则总览 KPI 会显示「命中 0% / ¥0」。
    """
    empty = _FakeInsightStore(agg=_empty_agg(), runs=[])
    insight = context_stats.build_context_insight(days=7, store=empty)
    assert insight["kpi"]["cache_hit_ratio"] is None
    assert insight["kpi"]["cost"] is None

    unpriced = _FakeInsightStore(
        agg=_empty_agg(total_tokens=500, input_tokens=400, output_tokens=100,
                       cost_unpriced_calls=3, cache_hit_ratio=0.5, llm_calls=3),
        runs=[_run("r1", "x", "success", 1)])
    got = context_stats.build_context_insight(days=7, store=unpriced)
    assert got["kpi"]["cost"] is None          # 有调用但全部未计价 ≠ ¥0
    assert got["kpi"]["cache_hit_ratio"] == 0.5


def test_insight_reports_service_cost_as_separate_tristate():
    """按次计费服务（Search/Fetch）与 LLM 成本**并列但不同口径**，三态各自成立。

    ⚠️ 三态不能合并：`service_calls == 0` 是「区间内没用过」（不是未计价、也不是 ¥0），
    有调用但全未计价才是 None。合并会让「没用过」被读成「不要钱」（铁律 3）。
    """
    # ① 有已计价的按次调用：金额照出，且**不并入** kpi.cost
    priced = _FakeInsightStore(
        agg=_empty_agg(llm_calls=1, total_tokens=100, input_tokens=100,
                       cost_total=0.25, cache_hit_ratio=0.5,
                       service_cost=0.015, service_cost_daily=[0.0, 0.015],
                       service_calls=3, service_unpriced_calls=0,
                       service_by_name={"deepseek-search": {"calls": 3, "cost": 0.015}}),
        runs=[_run()])
    kpi = context_stats.build_context_insight(days=7, store=priced)["kpi"]
    assert kpi["cost"] == 0.25
    assert kpi["service_cost"] == 0.015
    assert kpi["service_calls"] == 3
    assert kpi["service_by_name"]["deepseek-search"]["calls"] == 3
    assert context_stats.build_context_insight(
        days=7, store=priced)["daily_service_cost"] == [0.0, 0.015]

    # ② 有调用但全部未计价 -> None（不是 0）
    unpriced = _FakeInsightStore(
        agg=_empty_agg(service_calls=2, service_unpriced_calls=2, service_cost=0.0),
        runs=[_run()])
    kpi2 = context_stats.build_context_insight(days=7, store=unpriced)["kpi"]
    assert kpi2["service_cost"] is None
    assert kpi2["service_calls"] == 2 and kpi2["service_unpriced_calls"] == 2

    # ③ 区间内压根没用 -> 0 次 + None 金额（UI 必须显「未使用」而不是「未计价」）
    none = _FakeInsightStore(agg=_empty_agg(), runs=[])
    kpi3 = context_stats.build_context_insight(days=7, store=none)["kpi"]
    assert kpi3["service_calls"] == 0 and kpi3["service_cost"] is None


def test_insight_cache_hit_ratio_uses_input_tokens_denominator():
    """命中率的分母是 input_tokens，**不是调用数**（通则 6：分母缺失/为 0 -> None）。

    可达路径（审查者实测的构造）：窗口内 llm 步**全部缺 token_usage**（全是 error 步即可），
    store.aggregate_llm_usage 仍 llm_calls += 1，而 input_tokens = 0 -> store.py:586 的
    round(cached/inp, 4) if inp > 0 else 0.0 回 0.0 -> 若判据按调用数，KPI 会显示「命中 0%」
    —— 正是铁律 3 要防的「未采集显示成 0」。
    变异验证：把判据改回 llm_calls -> 本用例第一条断言红（0.0 is not None）。
    """
    # 有调用、但没有任何 input token（分母缺失）：命中率必须是 None
    no_usage = _FakeInsightStore(
        agg=_empty_agg(llm_calls=2, input_tokens=0, cache_hit_ratio=0.0),
        runs=[_run()])
    assert context_stats.build_context_insight(
        days=7, store=no_usage)["kpi"]["cache_hit_ratio"] is None
    # 反向对照（分母存在就照算，哪怕分子是 0 —— 那是「实测 0% 命中」，不是「未采集」）
    zero_hit = _FakeInsightStore(
        agg=_empty_agg(llm_calls=2, input_tokens=1000, cache_hit_ratio=0.0),
        runs=[_run()])
    assert context_stats.build_context_insight(
        days=7, store=zero_hit)["kpi"]["cache_hit_ratio"] == 0.0


def test_insight_cost_is_zero_when_calls_are_priced_free():
    """免费 ≠ 0 元（AGENTS.md 成本三约束之三）：billing=free 是**已计价为 0**，给 0.0。

    变异验证：把 cost 守卫简化成「cost_total > 0 才给，否则 None」—— 它能让上面那条
    「全未计价 -> None」用例保持绿色，却把「免费」也一起显示成「未计价」，本用例立刻红。
    判据必须是「区间内**存在已计价的调用**」（llm_calls 减去未计价、再减去从未记录），
    不是「金额大于 0」。
    """
    free = _FakeInsightStore(
        agg=_empty_agg(total_tokens=800, output_tokens=200, llm_calls=3,
                       cost_free_calls=3, cache_hit_ratio=0.25),
        runs=[_run()])
    got = context_stats.build_context_insight(days=7, store=free)
    assert got["kpi"]["cost"] == 0.0          # 免费 = 已计价为 0，仍是 0.0 而不是 None
    # 混合：2 次调用里 1 次未计价 + 1 次免费 -> 仍有已计价调用，金额口径成立（0.0）
    mixed = _FakeInsightStore(
        agg=_empty_agg(llm_calls=2, cost_unpriced_calls=1, cost_free_calls=1),
        runs=[_run()])
    assert context_stats.build_context_insight(days=7, store=mixed)["kpi"]["cost"] == 0.0


def test_insight_cost_is_none_when_cost_never_recorded():
    """第三态「从未记录」：extra 里**根本没有 cost 字段** -> None，不是 0.0（免费）。

    ⚠️ 可达路径不是假设：COST_TRACKING_ENABLED=false 期间（config.py 里的受支持配置）落下的
    行、以及未回填的历史行（scripts/ops/backfill_costs.py 的存在即证明有这类行）。
    store.aggregate_llm_usage 对这类行**两侧都不入账**（既不加 cost_total，也不加
    cost_unpriced_calls），所以 llm_calls - cost_unpriced_calls 会把它误当成「已计价」，
    kpi.cost 显示 0.0 = 「免费」。同文件的 build_run_context 用 cost_seen 对同一形态给 None
    —— 同一份数据两个入口必须同结论。
    变异验证：① 判据漏掉 - cost_missing_calls -> 本用例红（0.0 is not None）；
    ② store 侧不再计 cost_missing_calls -> 本用例红（同一份数据又变回「已计价」）。
    """
    never = _FakeInsightStore(
        agg=_empty_agg(llm_calls=3, cost_missing_calls=3, cost_total=0.0),
        runs=[_run()])
    assert context_stats.build_context_insight(days=7, store=never)["kpi"]["cost"] is None
    # 混合：3 次调用 = 1 未计价 + 2 从未记录 -> 没有任何已计价调用，仍是 None
    mixed = _FakeInsightStore(
        agg=_empty_agg(llm_calls=3, cost_unpriced_calls=1, cost_missing_calls=2),
        runs=[_run()])
    assert context_stats.build_context_insight(days=7, store=mixed)["kpi"]["cost"] is None
    # 对照：有 1 次已计价（其余未计价 / 从未记录）-> 金额口径成立
    partial = _FakeInsightStore(
        agg=_empty_agg(llm_calls=3, cost_unpriced_calls=1, cost_missing_calls=1,
                       cost_total=0.25),
        runs=[_run()])
    assert context_stats.build_context_insight(days=7, store=partial)["kpi"]["cost"] == 0.25


def test_insight_run_window_filters_by_since():
    """runs 必须按 [now - days, now] 裁剪（Python 侧、字典序比较）。

    变异验证：删掉 >= since 过滤 -> 窗口外那条混进来（断言变 2 条，红）。
    两条时间都用 ±1h 余量构造，不依赖执行时刻。
    """
    inside = _run("inside", created_at=_iso_ago(days=6, hours=23))   # 边界内 1h
    outside = _run("outside", created_at=_iso_ago(days=7, hours=1))  # 边界外 1h
    fake = _FakeInsightStore(agg=_empty_agg(), runs=[inside, outside])
    got = context_stats.build_context_insight(days=7, store=fake)
    assert [r["id"] for r in got["runs"]] == ["inside"]
    assert got["kpi"]["runs"] == 1
    # 同一批 runs 放到 30 天窗口里两条都在 —— 窗口由 days 驱动，不是恒等裁剪
    wider = context_stats.build_context_insight(days=30, store=fake)
    assert [r["id"] for r in wider["runs"]] == ["inside", "outside"]


def _frac_digits(stamp: str) -> int:
    """ISO 8601 时间戳的小数秒**位宽**（无小数 -> 0）：只量位宽，不看值、不看时区写法。"""
    body = re.sub(r"(?:Z|[+-]\d{2}:\d{2})$", "", stamp)
    head, _, frac = body.partition(".")
    assert len(head) == 19, stamp           # YYYY-MM-DD?HH:MM:SS 骨架恒 19 位
    assert frac == "" or frac.isdigit(), stamp
    return len(frac)


def test_insight_since_matches_producer_timestamp_shape():
    """since 必须与**产出端**（models._now）同形状 —— 否则字典序比较会静默误裁边界日。

    实测依据（本任务取证，勿猜）：runs.created_at 的唯一产出点是
    utils/agent_trace/models.py::_now = datetime.now(timezone.utc).isoformat()，
    500/500 条真实样本均为 'T' 分隔、6 位小数、时区值 UTC；且 store.aggregate_llm_usage
    本来就拿同一个 since 做 SQL 字符串比较（WHERE created_at>=?）—— 字典序即时间序。

    ⚠️ 断言的是**性质**（时区的值 / 分隔符 / 位宽），不是产出端的偶然拼写：
      * 尾缀写 'Z' 与 '+00:00' 在字典序下**等价**（都不误裁），故本用例对 'Z' 必须**绿**
        —— 硬编码 '+00:00' 会红，那是把「另一个模块的拼写」当契约（过约束，红的原因与
        它自称守护的性质无关）。实测边界 4 小时内：尾缀变体 0 误裁，空格变体误裁 289 分钟。
      * 真正会出事的三条：① 时区的**值**不是 UTC（裸时间 / +08:00）；
        ② 第 10 位分隔符不是同一字符（同一天里 ' '(0x20) < 'T'(0x54)，会被按**整日**误裁）；
        ③ 小数位宽不一致（边界处在一个截断单位内错判）。
    变异验证：把 models._now 改成空格分隔 -> 本用例红；改成尾缀 'Z' -> 绿（性质断言的收益）；
    把下面 produced 三条断言删掉 -> 空格变异下转绿（证明那三条是承重的）。
    """
    from utils.agent_trace.models import _now

    fake = _FakeInsightStore(agg=_empty_agg(), runs=[])
    since = context_stats.build_context_insight(days=7, store=fake)["range"]["since"]

    # 1) 自形状 + 窗口口径：tz-aware，且下界就是 now - days
    parsed = datetime.fromisoformat(since)
    assert parsed.utcoffset() is not None                 # tz-aware，不是裸时间
    assert abs((datetime.now(timezone.utc) - parsed) - timedelta(days=7)) < timedelta(seconds=60)
    # 同一个 since 必须同时用于聚合与返回体：日线（daily / cost_daily）与 runs 裁剪
    # 不许各算一遍 now，否则「标题写 7 天、日线是 7 天零几毫秒」这类口径错位无法察觉
    assert fake.calls[0][0] == "aggregate_llm_usage"
    assert fake.calls[0][1]["since"] == since
    assert fake.calls[0][1]["days"] == 7

    # 2) 与产出端真实产物**同性质** —— 这一段才是防产出端漂移的承重部分
    produced = _now()
    p, s = datetime.fromisoformat(produced), datetime.fromisoformat(since)
    # 口径是「UTC 的值」，不是 '+00:00' 的写法：'Z' 也过，裸时间 / +08:00 红
    assert p.utcoffset() == s.utcoffset() == timedelta(0)
    # 分隔符：唯一会按「整日」误裁的一位（空格变更红）
    assert produced[10] == since[10]
    # 小数位宽：混精度会在边界一个截断单位内错判
    assert _frac_digits(produced) == _frac_digits(since)


def test_insight_clamps_days_and_forwards_the_same_value():
    """days 裁到 >= 1，且必须把**裁剪后的同一个值**传给 aggregate_llm_usage。

    变异验证：删掉 max(1, int(days)) -> days=0 时 range.days 变 0，且真实 store 里
    max(1, int(days or 14)) 会把 0 当「没传」静默变成 14 天 —— 于是「标题 0 天、
    日线 14 桶」，本用例两条断言同时红。
    """
    for raw, expected in ((0, 1), (-5, 1), (7, 7), ("3", 3)):
        fake = _FakeInsightStore(agg=_empty_agg(), runs=[])
        got = context_stats.build_context_insight(days=raw, store=fake)
        assert got["range"]["days"] == expected, raw
        assert fake.calls[0][1]["days"] == expected, raw


def test_insight_tolerates_missing_aggregates_and_runs():
    """聚合结果缺失 / list_runs 返回 None 一律不抛，且不编造数字（铁律 3）。"""
    for agg in ({}, None):
        fake = _FakeInsightStore(agg=agg, runs=None)
        got = context_stats.build_context_insight(days=7, store=fake)
        assert got["kpi"] == {"runs": 0, "billed_tokens": 0, "cost": None,
                              "cache_hit_ratio": None, "reasoning_tokens": 0,
                              "answer_tokens": 0,
                              # 按次计费服务（2026-10-18 加）：聚合缺失时是「没用过/未采集」，
                              # 不是 ¥0（金额字段给 None）
                              "service_cost": None, "service_calls": 0,
                              "service_unpriced_calls": 0, "service_by_name": {}}
        assert got["daily_tokens"] == [] and got["daily_cost"] == []
        assert got["daily_service_cost"] == []
        assert got["by_model"] == [] and got["runs"] == []


def test_insight_runs_carry_only_the_contract_fields():
    """runs 只投影 5 个字段：真实 store.list_runs 是 SELECT *（input / output / error
    单条可达几十 KB），总览大屏的 SSE payload 不得把它们带出去。"""
    row = dict(_run("r1"), input="x" * 10, output="y" * 10, error="boom",
               parent_run_id="p0")
    fake = _FakeInsightStore(agg=_empty_agg(), runs=[row])
    got = context_stats.build_context_insight(days=7, store=fake)
    assert set(got["runs"][0]) == {"id", "agent_name", "status", "duration_ms",
                                   "created_at"}
    assert got["runs"][0]["id"] == "r1"
    assert got["runs"][0]["duration_ms"] == 100


def test_insight_thinking_is_clamped_to_output():
    """脏数据（思考 > 输出）按输出封顶：answer 不为负，拆分恒等式不破（铁律 2）。"""
    fake = _FakeInsightStore(
        agg=_empty_agg(output_tokens=100, reasoning_tokens=400, llm_calls=1,
                       cache_hit_ratio=0.5, cost_total=0.2),
        runs=[_run()])
    got = context_stats.build_context_insight(days=7, store=fake)
    assert got["kpi"]["reasoning_tokens"] == 100
    assert got["kpi"]["answer_tokens"] == 0
    assert got["kpi"]["reasoning_tokens"] + got["kpi"]["answer_tokens"] == 100


def test_events_tolerate_inf_lengths():
    """长度兜底必须覆盖 OverflowError：int(float("inf")) 抛的是 OverflowError。

    为什么可达：extra 是自由 JSON，json.dumps(float("inf")) 会写出 Infinity 字面量
    （Python json 的非标准扩展，json.loads 能读回 inf），于是 DB 理论上可出现。
    当前产出端（agents/react/tool_node_base.py:222-226）用 len() 恒 int、真实不可达 ——
    属于「存在合法输入能改行为、只是产出端不产生」的等价类，必须用合成输入钉住，
    否则产出端一改就是静默 500（上下文事件整块解析失败）。
    变异验证：把 except 收回 (TypeError, ValueError) -> 本用例第一组直接 OverflowError。
    """
    for bad in (float("inf"), float("-inf"), float("nan")):
        for field in ("raw_len", "final_len"):
            payload = {"raw_len": 10, "final_len": 4, "compress_method": "trim"}
            payload[field] = bad
            # allow_nan=True 是 json 默认：写出 Infinity / -Infinity / NaN 字面量
            step = _step("tool", 1, extra=json.dumps({"compress": payload}))
            events = context_stats.build_events([step])
            assert [e["kind"] for e in events] == ["compress"], (field, bad)
            assert events[0]["delta_chars"] is None, (field, bad)
    # 题面给的形态：只有 raw_len 是 inf（final_len 缺失 -> 兜底同样要给 None，不抛）
    step = _step("tool", 1, extra=json.dumps(
        {"compress": {"raw_len": float("inf"), "compress_method": "trim"}}))
    events = context_stats.build_events([step])
    assert [e["kind"] for e in events] == ["compress"]
    assert events[0]["delta_chars"] is None


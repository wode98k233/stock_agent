"""仪表盘 system 前缀必须与 report-fast 一致 —— 全模式回归护栏

**事故背景（2026-10-13）**
PRD-A9 §3.1 把 report-fast / report-select / dashboard 三条调用统一到
``build_shared_system(template)``。但 ``template`` 参数的可达性在各模式间**不一致**：

| 模式 | 报告 build() | 仪表盘入口 | template 原本能传到吗 |
|---|---|---|---|
| react_stock | shared ReportEnhanceNode | dashboard_node_impl | ✅ 自己写进 state |
| plan_solve / unified_plan | shared | graph 的 dashboard_node | ❌ 只有 template_id |
| pdor | shared | graph 的 dashboard_node | ❌ 只有 template_id |
| agent_group | shared | **直调** append_dashboard | ❌ 不传 |
| scenario | shared | **直调** append_dashboard | ❌ 不传 |

未传时 ``build_shared_system({})`` 只生成 450B 残缺 system，而 report-fast 用的是
3549B 完整模板 —— **前缀完全不同，仪表盘缓存收益为零**。用户在生产用 unified_plan，
这个模式当时完全没吃到 P1 的收益。

本测试**不调 LLM**（纯字符串拼接 + monkeypatch），因此不消耗任何数据源配额。
"""
import hashlib

import pytest


def _sys_fp(messages) -> tuple:
    text = "".join(c for r, c in messages if r == "system")
    return hashlib.sha256(text.encode()).hexdigest()[:16], len(text)


@pytest.fixture(scope="module")
def market_template():
    from agents.analysis.template_store import load_template
    return load_template("market_daily")


# ── 指纹层：不依赖任何 state，纯比较构造逻辑 ──────────────────────────

def test_report_fast_system_is_substantial(market_template):
    """基线：report-fast 的 system 不应异常短，否则下面的比对没有意义。"""
    from agents.analysis.report_builder import _build_prompt
    _, size = _sys_fp(_build_prompt(market_template, {}, "今天大盘总结", raw_result="草稿"))
    assert size > 1000, f"report-fast system 异常短({size}B)，模板可能没加载到"


def test_dashboard_with_template_shares_report_fast_prefix(market_template):
    """React 路径（template 已到位）：指纹一致。"""
    from agents.analysis.report_builder import _build_prompt as rb
    from agents.analysis.dashboard_generator import _build_prompt as dash

    fast_fp, _ = _sys_fp(rb(market_template, {}, "今天大盘总结", raw_result="草稿"))
    dash_fp, _ = _sys_fp(dash("报告正文", {}, "今天大盘总结", "市场情绪",
                              template=market_template))
    assert dash_fp == fast_fp


def test_dashboard_without_template_diverges(market_template):
    """反证：template=None 时确实不同 —— 这就是修复前 plan/pdor/group/scenario 的行为。"""
    from agents.analysis.report_builder import _build_prompt as rb
    from agents.analysis.dashboard_generator import _build_prompt as dash

    fast_fp, fast_size = _sys_fp(rb(market_template, {}, "q", raw_result="d"))
    none_fp, none_size = _sys_fp(dash("报告正文", {}, "q", "市场情绪", template=None))
    assert none_fp != fast_fp
    assert none_size < fast_size * 0.5, (
        f"残缺 system 未如预期显著变短：{none_size} vs {fast_size}"
    )


# ── 入口层：覆盖全部 5 种模式的实际调用方式（monkeypatch，不调 LLM）──────

async def _capture_append_dashboard(monkeypatch, **kwargs):
    """调 append_dashboard，返回它最终传给 dashboard_generate 的 template。"""
    from agents.analysis import dashboard_node as dn_mod

    seen = {}

    async def fake_generate(**kw):
        seen["template"] = kw.get("template")
        seen["template_id"] = kw.get("template_id")
        return None

    orig = dn_mod.dashboard_generate
    dn_mod.dashboard_generate = fake_generate
    try:
        await dn_mod.append_dashboard(
            report="## 今日概览\n" + "报告正文。" * 20,
            slot_results={"mx_data_query": {"rows": 3}},
            template_id=kwargs.pop("template_id", "market_daily"),
            user_input="今天大盘总结",
            **kwargs,
        )
    finally:
        dn_mod.dashboard_generate = orig
    return seen


async def test_plan_unified_and_pdor_entry_gets_template(monkeypatch, market_template):
    """plan_solve / unified_plan / pdor：经 graph 的 dashboard_node → append_dashboard。

    这三个模式的 state 里只有 template_id，没有 selected_template，
    修复前 append_dashboard 收到 template=None。
    """
    from agents.analysis.dashboard_node import dashboard_node_impl

    captured = {}

    async def fake_append(**kw):
        captured.update(kw)
        return kw["report"]

    from agents.analysis import dashboard_node as dn_mod
    orig = dn_mod.append_dashboard
    dn_mod.append_dashboard = fake_append
    try:
        state = {
            "response": "## 今日概览\n报告正文" * 5,
            "template_id": "market_daily",        # 只有这个键
            "tool_calls": [],
            "logger": None,
        }
        assert "selected_template" not in state
        await dashboard_node_impl(state)
    finally:
        dn_mod.append_dashboard = orig

    assert "template" in captured, "append_dashboard 未被调用（category 解析失败？）"
    # 节点层不传模板（保持选型逻辑不变），兜底发生在 append_dashboard 内部
    seen = await _capture_append_dashboard(
        monkeypatch, template_id="market_daily", template=None)
    assert seen["template"] is not None, "append_dashboard 未对 template 做兜底"
    assert seen["template"].get("sections")


async def test_group_and_scenario_direct_entry_gets_template(monkeypatch, market_template):
    """agent_group / scenario：直调 append_dashboard 且不传 template。

    这两个调用点根本没走 dashboard_node_impl，所以兜底必须落在 append_dashboard
    自身才能覆盖（2026-10-13 修复前的 P1 只改了 dashboard_node，漏掉了这两处）。
    """
    seen = await _capture_append_dashboard(monkeypatch)   # 模拟直调、template 缺省
    assert seen["template"] is not None, "直调路径未拿到模板"
    assert seen["template"].get("id") == market_template.get("id")


async def test_explicit_template_still_wins(monkeypatch, market_template):
    """显式传入的 template 不应被兜底覆盖。"""
    sentinel = {"id": "sentinel", "sections": [{"title": "X"}]}
    seen = await _capture_append_dashboard(monkeypatch, template=sentinel)
    assert seen["template"] is sentinel


async def test_get_dashboard_category_argument_unchanged(monkeypatch):
    """防回归：兜底不得改变仪表盘选型逻辑的入参。

    唯一会受影响的模板是 hot_news（无 associations 映射、但带 dashboard_type），
    修复后仍应走默认 stock —— 选型变更不属于本 PRD 范围。
    """
    from agents.analysis import dashboard_node as dn_mod

    captured = []
    orig_cat = dn_mod.get_dashboard_category

    def spy(template_id, template=None):
        captured.append(template)
        return orig_cat(template_id, template=template)

    dn_mod.get_dashboard_category = spy
    try:
        await _capture_append_dashboard(monkeypatch, template_id="hot_news", template=None)
    finally:
        dn_mod.get_dashboard_category = orig_cat

    assert captured and captured[0] is None, (
        f"get_dashboard_category 收到了 template={captured[0]!r}，选型行为可能被改变"
    )
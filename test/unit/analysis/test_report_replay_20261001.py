"""
事故回放测试 — 2026-10-01 agent_group（req=d557f47f）

fixture 是**日志原文**（非构造），固化了以下真实事实：
  - data_doc：数据底版真实内容 3000 字符，含 399001.SZ / -8.044% / 4294亿 / 1.409；
  - step_results[0]（data_collector）的 result 为 **0 字符** —— 被失败的补采覆盖；
  - step_results[1..4]：4 份同构分析师报告（各约 4.5~5.3k 字符）。

事故本质：报告写作节点最终一份原始数据都没拿到，只能改写 4 份散文，
于是把「深成最大回撤 −7.31%」写成了「深成月涨跌幅」（底版实为 −8.044%）。

本测试把「报告层能否拿到原始数据」变成可断言的行为：
  1) 走 group 的条目通道（_build_evidence_items → tool_evidence）→ 必须命中底版指纹；
  2) 走旧路径（只传 step_results、无条目证据）→ 底版指纹不得出现。
     第 2 条把事故特征钉死：若失败，说明 report_builder 的通道判断被改回去了。

运行: pytest test/unit/analysis/test_report_replay_20261001.py -v
"""
import json
import os

import pytest
from unittest.mock import MagicMock, patch

_FX = os.path.join(os.path.dirname(__file__), "fixtures", "report_input_d557f47f.json")


@pytest.fixture
def accident() -> dict:
    with open(_FX, encoding="utf-8") as f:
        return json.load(f)


def _plan_from(accident: dict) -> list:
    return [
        {
            "step": s["step"],
            "agent_names": [s["skill"]],
            "task_purpose": s["purpose"],
            "result": s["result"],
        }
        for s in accident["step_results"]
    ]


def _joined(messages: list) -> str:
    return "\n".join(content for _, content in messages)


# ══════════════════════════════════════════════════════════════
# 1. 条目构造（group 产物 → 可下钻条目）
# ══════════════════════════════════════════════════════════════

def test_evidence_items_put_data_doc_first_and_skip_empty(accident):
    """数据底版单列首条；空 result 的步（事故中的 Step1）不产生空条目。"""
    from agents.group.nodes.report_enhance import _build_evidence_items

    items = _build_evidence_items(_plan_from(accident), accident["data_doc"], accident["real_tool_calls"])

    assert items, "必须产出条目（修复前这里传的是空列表）"
    assert items[0]["tool_name"] == "数据底版 · 数据采集员"
    assert len(items[0]["tool_output"]) == len(accident["data_doc"])
    # 无空条目 —— Step1 的 result 是 0 字符，不应成为条目
    assert all(it["tool_output"] for it in items)
    assert not any(it["tool_name"] == "分析 · data_collector" for it in items)
    # 4 个分析师的报告 + 2 条真实工具调用都在
    assert sum(1 for it in items if it["tool_name"].startswith("分析 · ")) == 4
    assert sum(1 for it in items if it["tool_name"] == "mx_data_query") == 2


def test_evidence_items_do_not_duplicate_doc_identical_step(accident):
    """某步 result 与数据底版完全相同时不重复成条。"""
    from agents.group.nodes.report_enhance import _build_evidence_items

    plan = _plan_from(accident)
    plan[0]["result"] = accident["data_doc"]          # 模拟「底版由 Step1 产出」的正常情形

    items = _build_evidence_items(plan, accident["data_doc"], [])

    same = [it for it in items if it["tool_output"] == accident["data_doc"]]
    assert len(same) == 1


# ══════════════════════════════════════════════════════════════
# 2. 修复后：原始数据能进报告 prompt
# ══════════════════════════════════════════════════════════════

def test_group_channel_carries_raw_data_into_report_prompt(accident):
    """条目通道（tool_evidence）必须把数据底版带进报告 prompt。"""
    from agents.group.nodes.report_enhance import _build_evidence_items
    from agents.analysis.report_builder import _build_prompt, _format_tool_calls
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template(accident["template_id"]), {})
    items = _build_evidence_items(_plan_from(accident), accident["data_doc"], accident["real_tool_calls"])
    # 模拟 build() 的下钻装配：未选中 / 下钻关闭 / 证据过小时退回全量条目
    tool_evidence = _format_tool_calls(items)

    messages = _build_prompt(
        template, {}, accident["user_input"],
        raw_result=accident["data_doc"],
        tool_evidence=tool_evidence,
    )
    joined = _joined(messages)

    for fp in accident["fingerprints"]:
        assert fp in joined, f"原始数据指纹 {fp!r} 未进入报告 prompt"
    assert "原始工具数据" in joined


def test_report_prompt_does_not_duplicate_raw_result_when_evidence_present(accident):
    """group 声明 raw_result_is_seed 且条目证据已载入时，raw_result 不再重复注入。"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template(accident["template_id"]), {})
    tool_evidence = "## 原始工具数据\n" + accident["data_doc"]

    messages = _build_prompt(
        template, {}, accident["user_input"],
        raw_result=accident["data_doc"],
        tool_evidence=tool_evidence,
        raw_result_is_seed=True,
    )
    assert _joined(messages).count("399001.SZ") == 1


def test_other_modes_still_inject_raw_result(accident):
    """对照：React / Unified 形态（step_results 空 + 有证据）必须照旧注入 raw_result。

    这两个模式的 raw_result 是报告草稿（state["final_result"] / response），
    挡掉它会让最终报告丢掉主体内容 —— 2026-10-01 一度写成 `elif not tool_evidence`
    时就是这个问题，故在此钉死。
    """
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template(accident["template_id"]), {})
    messages = _build_prompt(
        template, {}, accident["user_input"],
        raw_result=accident["data_doc"],
        tool_evidence="## 原始工具数据\n" + accident["data_doc"],
        step_results=[],
        raw_result_is_seed=False,          # React / Unified / Plan / PDOR 默认
    )
    assert "## 原始素材" in _joined(messages)


# ══════════════════════════════════════════════════════════════
# 3. 事故特征钉死：旧路径拿不到原始数据
# ══════════════════════════════════════════════════════════════

def test_legacy_step_only_channel_drops_raw_data(accident):
    """事故特征：只给 step_results（无条目/工具证据）时 raw_result 不进 prompt。

    这正是 2026-10-01 报告零原始数据的原因。本断言失败 = 通道判断被改回去，
    事故会复现（报告又开始从散文里编数值）。
    """
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template(accident["template_id"]), {})
    step_results = [
        {"step": s["step"], "skill": s["skill"], "purpose": s["purpose"], "result": s["result"]}
        for s in accident["step_results"]
    ]

    messages = _build_prompt(
        template, {}, accident["user_input"],
        raw_result=accident["data_doc"],
        step_results=step_results,
    )
    joined = _joined(messages)

    assert not any(fp in joined for fp in accident["fingerprints"]), \
        "旧路径不应携带数据底版指纹（若通过，说明已经修好了 raw_result 的注入条件）"
    # Step1 只剩任务描述（事故原状：0 字符 result）
    assert "### Step 1" in joined
    assert accident["step_results"][0]["result"] == ""


# ══════════════════════════════════════════════════════════════
# 4. 节点级：report_enhance 传给引擎的参数
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_report_enhance_passes_items_and_keeps_doc_as_seed(accident):
    """report_enhance_node → run_report_post_processing 的实参必须是「条目 + 底版种子」。"""
    from agents.group.nodes.report_enhance import report_enhance_node

    state = {
        "input": accident["user_input"],
        "plan_steps": _plan_from(accident),
        "data_collection_doc": accident["data_doc"],
        "accumulated_data": "### macro_analyst 分析结果\n" + accident["step_results"][1]["result"],
        "tool_calls": accident["real_tool_calls"],
        "template_id": accident["template_id"],
        "selected_skills": [],
        "dialog_uuid": "u",
        "task_id": "t",
    }
    ctx = MagicMock()
    ctx.budget = MagicMock()
    ctx.progress_reporter = MagicMock()
    captured = {}

    async def fake_post(**kwargs):
        captured.update(kwargs)
        return None

    with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
         patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
         patch("agents.group.nodes.report_enhance.run_report_post_processing", side_effect=fake_post):
        mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
        mock_cfg.DASHBOARD_ENABLED = False
        mock_log_cls.from_state.return_value = MagicMock()
        await report_enhance_node(state, ctx)

    assert captured["tool_calls"], "修复前这里是空列表 → 报告层零原始数据"
    assert captured["tool_calls"][0]["tool_name"] == "数据底版 · 数据采集员"
    # 已表达为条目，不再走 step_results 双通道
    assert captured["step_results"] is None
    # raw_result 仅作下钻种子（不直接进 prompt），用底版
    assert captured["raw_result"] == accident["data_doc"]
    # 该标记必须显式传给引擎，否则 raw_result 会与条目通道重复注入
    assert captured["raw_result_is_seed"] is True
    # 真实工具调用一并交给报告按需回捞
    assert any(it["tool_name"] == "mx_data_query" for it in captured["tool_calls"])


@pytest.mark.asyncio
async def test_report_enhance_falls_back_to_accumulated_without_doc(accident):
    """无数据底版时，raw_result 退化为 accumulated（保底不空）。"""
    from agents.group.nodes.report_enhance import report_enhance_node

    plan = _plan_from(accident)
    plan[0]["result"] = "分析结论" * 20
    state = {
        "input": accident["user_input"],
        "plan_steps": plan,
        "data_collection_doc": "",
        "accumulated_data": "累计结果文本" * 20,
        "tool_calls": [],
        "template_id": accident["template_id"],
        "selected_skills": [],
        "dialog_uuid": "u",
        "task_id": "t",
    }
    ctx = MagicMock()
    ctx.budget = MagicMock()
    ctx.progress_reporter = MagicMock()
    captured = {}

    async def fake_post(**kwargs):
        captured.update(kwargs)
        return None

    with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
         patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
         patch("agents.group.nodes.report_enhance.run_report_post_processing", side_effect=fake_post):
        mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
        mock_cfg.DASHBOARD_ENABLED = False
        mock_log_cls.from_state.return_value = MagicMock()
        await report_enhance_node(state, ctx)

    assert captured["raw_result"] == "累计结果文本" * 20
    assert captured["tool_calls"], "底版缺失时仍应把各步结果作为条目给出"
    assert not any(it["tool_name"].startswith("数据底版") for it in captured["tool_calls"])

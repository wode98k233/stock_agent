"""归因双字段 + fork 埋点的回归护栏（PRD-A11 FR-7）。

要锁死两件事：

1. ``purpose``（细分用途）与 ``node``（图节点名）**互不覆盖**——
   PRD-A10 §2.5.4 的事故就是 ``setdefault("node", ...)`` 撞上 LangGraph 已有的
   ``node``，把 ``report-fast`` / ``report-select`` 静默丢弃，三类调用混成一个名。
2. fork 埋点字段能一路从 metadata 落到 ``steps.extra``，且没给时**不写**空字段。
"""
from __future__ import annotations

from utils.agent_trace.core import build_llm_step_extra
from utils.logger import with_trace_node

PARAMS = {"model_name": "deepseek-v4.1-flash", "temperature": 0.0}


# ── with_trace_node：purpose / node 分家 ────────────────────────────────
def test_purpose_set_when_no_langgraph_node():
    m = with_trace_node(None, "report-fast")
    assert m["purpose"] == "report-fast"
    assert m["node"] == "report-fast"          # 无图节点名时回落，看板行为不变


def test_langgraph_node_does_not_swallow_purpose():
    """核心事故场景：LangGraph 已注入 node=report_enhance，调用方想标 report-fast。"""
    m = with_trace_node({"node": "report_enhance"}, "report-fast")
    assert m["node"] == "report_enhance"       # 图节点名：既有看板继续看得到
    assert m["purpose"] == "report-fast"       # 细分用途：不再被吃掉


def test_existing_purpose_wins():
    m = with_trace_node({"purpose": "report-select"}, "report-fast")
    assert m["purpose"] == "report-select"


def test_caller_dict_not_mutated():
    src = {"node": "report_enhance"}
    with_trace_node(src, "report-fast")
    assert src == {"node": "report_enhance"}


# ── build_llm_step_extra：落库形态 ──────────────────────────────────────
def test_purpose_and_node_land_in_extra_separately():
    extra = build_llm_step_extra(PARAMS,
                                 with_trace_node({"node": "report_enhance"}, "report-fast"))
    assert extra["purpose"] == "report-fast"
    assert extra["node"] == "report_enhance"
    assert extra["label"] == "生成报告"          # label 取 purpose（见 _GRAPH_NODE_LABELS）


def test_falls_back_to_node_when_no_purpose():
    """老调用方只传 node（不带 purpose）→ 行为必须与改动前完全一致。"""
    extra = build_llm_step_extra(PARAMS, {"node": "skill-router"})
    assert extra["node"] == "skill-router"
    assert extra["label"] == "skill-router"
    assert "purpose" not in extra


def test_langgraph_node_alias_supported():
    extra = build_llm_step_extra(PARAMS, {"langgraph_node": "agent"})
    assert extra["node"] == "agent"
    assert extra["label"] == "Agent 思考"


def test_no_attribution_fields_when_metadata_empty():
    extra = build_llm_step_extra(PARAMS, {})
    assert set(extra) == {"model_name", "temperature"}


def test_model_params_are_carried():
    extra = build_llm_step_extra({"model_name": "m", "max_tokens": 4096}, {})
    assert extra["model_name"] == "m"
    assert extra["max_tokens"] == 4096


# ── fork 埋点 ───────────────────────────────────────────────────────────
def test_fork_fields_are_written():
    from agents.shared.llm_fork import fork_metadata
    from agents.shared.prompt_prefix import make_fork_prefix

    prefix = make_fork_prefix([{"role": "system", "content": "S" * 100}],
                              [{"t": 1}], source="exec_state")
    meta = fork_metadata(prefix, ([], [{"t": 1}]))
    extra = build_llm_step_extra(PARAMS, {**with_trace_node(None, "dashboard"), **meta})
    assert extra["fork_kind"] == "llm_fork"
    assert extra["fork_prefix_chars"] == 100
    assert extra["fork_prefix_msgs"] == 1
    assert extra["fork_tools"] == 1
    assert extra["fork_source"] == "exec_state"
    assert "fork_skip_reason" not in extra      # 成功 fork 不写跳过原因


def test_fork_skip_reason_is_written_when_not_forked():
    from agents.shared.llm_fork import fork_metadata
    from agents.shared.prompt_prefix import ForkPrefix

    meta = fork_metadata(ForkPrefix(messages=[{"role": "system", "content": "x"}], tools=[]), None)
    extra = build_llm_step_extra(
        PARAMS, {**with_trace_node(None, "react-context-summary"), **meta})
    assert extra["fork_kind"] == "standalone"
    assert extra["fork_skip_reason"] == "no_tools"


def test_no_fork_fields_when_metadata_has_none():
    """绝大多数调用没有 fork 概念 → 不该往 extra 塞空字段（否则月库全是噪声）。"""
    extra = build_llm_step_extra(PARAMS, with_trace_node(None, "agent"))
    assert not any(k.startswith("fork_") for k in extra)


def test_compress_reason_passthrough():
    extra = build_llm_step_extra(
        PARAMS, {**with_trace_node(None, "react-context-summary"), "compress_reason": "soft_limit"})
    assert extra["compress_reason"] == "soft_limit"


def test_a8_fields_still_passthrough():
    """本次改动不能把 A8 已有字段挤掉（cache_hit 命中步骤靠它识别）。"""
    extra = build_llm_step_extra(PARAMS, {"node": "agent", "cache_hit": True,
                                          "saved_tokens": 1024})
    assert extra["cache_hit"] is True
    assert extra["saved_tokens"] == 1024

# ── 工具执行内部 / 包装节点内部的调用必须自带 purpose ────────────────────
# 背景：这些调用跑在**包装节点**（template_report / report_enhance）或一级图节点
# （tools）内部，直接沿用 ctx 的 metadata 会让 trace 里只剩包装节点名。
# 实测 70 次 sentiment-extract 全挤在 `template_report ← 模板报告` 下，看不出是谁。
def test_wrapper_node_children_keep_their_own_identity():
    """用真实调用点验证：包装节点名保留在 node，细分用途进 purpose。"""
    from utils.logger import with_trace_node
    meta = with_trace_node({"node": "template_report"}, "sentiment-extract")
    extra = build_llm_step_extra(PARAMS, meta)
    assert extra["node"] == "template_report"      # 图节点名，看板兼容
    assert extra["purpose"] == "sentiment-extract"  # 细分用途，归因可见
    # label 取 purpose，再经 _GRAPH_NODE_LABELS 翻成中文（有映射就用映射）
    from utils.agent_trace.core import _GRAPH_NODE_LABELS
    assert extra["label"] == _GRAPH_NODE_LABELS.get("sentiment-extract", "sentiment-extract")


def test_tool_internal_calls_get_purpose():
    """工具执行内部的 LLM 调用（aggregator / sentiment）：node 是 `tools`，全靠 purpose 区分。"""
    from utils.logger import with_trace_node
    for purpose in ("filter", "rank", "macro-report", "report",
                    "tech_interpret", "sentiment"):
        extra = build_llm_step_extra(PARAMS, with_trace_node(None, purpose))
        assert extra["purpose"] == purpose
        assert extra["node"] == purpose


def test_call_sites_actually_pass_purpose():
    """静态断言：这几处调用点确实把 with_trace_node 写进去了。

    没有这条，后人重构掉 metadata 参数时不会有任何提示 —— 而症状只是
    "trace 里少了个字段"，极难在事后发现（这正是 FR-7 要修的原始问题）。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    checks = [
        ("agents/analysis/extractors.py", "sentiment-extract"),
        ("tools/sentiment.py", "sentiment"),
        ("tools/aggregator.py", "filter"),
        ("tools/aggregator.py", "tech_interpret"),
    ]
    for rel, purpose in checks:
        src = (root / rel).read_text(encoding="utf-8")
        assert "with_trace_node" in src, f"{rel} 未使用 with_trace_node"
        assert f'"{purpose}"' in src, f"{rel} 未标注 purpose={purpose}"

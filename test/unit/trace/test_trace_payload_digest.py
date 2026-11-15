"""steps.input / steps.output 摘要化与还原（trace 存储瘦身 T1）。

设计：``docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md``

本文件只测纯函数（``utils/agent_trace/payload.py``），不碰 DB。
写入行为与体积护栏见 ``test_trace_write_slimming.py``。
"""
import json
import sys
from pathlib import Path

_project_root = str(Path(__file__).parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.agent_trace import payload


# ============================================================
# 构造"典型节点 state"：累积容器键占大头（与实测分布同构）
# ============================================================

def _big_state(**overrides):
    state = {
        # 累积容器键（实测占节点步 input 的 88%）
        "messages": [{"role": "human", "content": "M" * 5000} for _ in range(6)],
        "tool_calls": [{"name": "mx_data_query", "args": {"query": "q"}, "result": "R" * 9000}
                       for _ in range(4)],
        "dispatch_history": [{"round": i, "detail": "D" * 800} for i in range(30)],
        "plan_steps": [{"step": f"s{i}", "skill": "mx_data", "intent": "I" * 400}
                       for i in range(20)],
        "original_plan": {"steps": [{"i": i} for i in range(40)]},
        "accumulated_data": {"alpha": "A" * 9000},
        "step_results": [{"result": "S" * 3000} for _ in range(3)],
        "past_steps": [{"p": i} for i in range(20)],
        "key_data": {"k": "K" * 2000},
        "_fork_messages": [{"role": "ai", "content": "F" * 8000}],
        "_fork_tools": [{"name": "t", "desc": "T" * 3000}],
        "data_collection_doc": "C" * 4000,
        # 普通字段（必须原样保留）
        "current_step_index": 3,
        "template_id": "market_daily",
        "selected_template_id": "market_daily",
        "constraints": ["报告为收盘点评", "不输出个股买卖建议"],
        "budget_exempt": True,
        "input": "收盘啦，收盘啦，点评下今天大盘",
        "response": "B" * 2000,
    }
    state.update(overrides)
    return state


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


# ============================================================
# LLM prompt 摘要
# ============================================================

def test_llm_prompt_summary_is_reference_not_payload():
    """llm 步的 input 只存引用摘要：条数 / 字数 / 指纹 / 预览，不含全文。"""
    prompt = [[{"role": "system", "content": "S" * 40000}, {"role": "human", "content": "h"}]]
    summary = payload.summarize_llm_prompt(prompt)

    assert summary["_kind"] == "llm_prompt_ref"
    assert summary["msg_count"] == 2
    assert summary["chars"] > 40000
    assert len(summary["sha256"]) == 16
    assert len(summary["preview"]) == payload.LLM_PREVIEW_CHARS
    # 全文不进摘要：摘要体积必须远小于原文
    assert len(_dump(summary)) < 1500
    assert "S" * 5000 not in _dump(summary)


def test_llm_prompt_msg_count_handles_batched_and_flat_shapes():
    """兼容 on_chat_model_start 的 list[list[Message]] 与 on_llm_start 的 list[str]。"""
    assert payload.summarize_llm_prompt([["a", "b"], ["c"]])["msg_count"] == 3
    assert payload.summarize_llm_prompt(["a", "b"])["msg_count"] == 2
    assert payload.summarize_llm_prompt(None)["msg_count"] == 0


def test_restore_rebuilds_prompt_from_messages():
    """还原：用该步 messages 行重建，逐条与库里的 role/content 一致。"""
    messages = [
        {"role": "system", "content": "你是分析师"},
        {"role": "human", "content": "点评下今天大盘"},
        {"role": "ai", "content": "", "tool_calls": '[{"name": "mx_data_query", "args": {"query": "大盘"}}]'},
        {"role": "tool", "content": '{"up": 2274}', "tool_call_id": "call_1"},
    ]
    raw = _dump(payload.summarize_llm_prompt([["x"]]))
    restored = json.loads(payload.restore_step_text(raw, messages))

    assert [m["role"] for m in restored] == ["system", "human", "ai", "tool"]
    assert restored[1]["content"] == "点评下今天大盘"
    assert restored[2]["tool_calls"][0]["name"] == "mx_data_query"  # JSON 字符串被解析回结构
    assert restored[3]["tool_call_id"] == "call_1"


def test_restore_keeps_legacy_rows_untouched():
    """向后兼容：老格式行（prompt repr / 普通文本）原样返回，不需要全量迁移。"""
    legacy = '[\n  [\n    "content=\'x\' additional_kwargs={}",\n    "content=\'y\'"\n  ]\n]'
    assert payload.restore_step_text(legacy, []) == legacy
    assert payload.restore_step_text("纯文本输入", []) == "纯文本输入"
    assert payload.restore_step_text({"a": 1}, []) == {"a": 1}
    assert payload.restore_step_text(None, []) is None


# ============================================================
# 节点 state 摘要
# ============================================================

def test_digest_drops_accumulator_content():
    """累积容器键只记形状：条数 / 原始字符，绝不序列化内容。"""
    digest = payload.digest_state(_big_state())
    dumped = _dump(digest)

    assert digest["_kind"] == "state_digest"
    assert digest["_acc"]["tool_calls"]["count"] == 4
    assert digest["_acc"]["messages"]["count"] == 6
    assert digest["_acc"]["dispatch_history"]["count"] == 30
    assert digest["_acc"]["tool_calls"]["chars"] > 4 * 9000
    # 任何一个累积键的内容体都不许出现在摘要里
    for marker in ("M" * 1000, "R" * 1000, "D" * 500, "A" * 1000, "S" * 1000, "F" * 1000):
        assert marker not in dumped


def test_digest_keeps_scalars_verbatim():
    """标量原样保留 —— select_template 的 output.selected_template_id 被
    server/quality_service.py:573 直接消费，任何裁剪都不许动它。"""
    digest = payload.digest_state(_big_state())
    assert digest["selected_template_id"] == "market_daily"
    assert digest["template_id"] == "market_daily"
    assert digest["current_step_index"] == 3
    assert digest["budget_exempt"] is True
    assert digest["constraints"] == ["报告为收盘点评", "不输出个股买卖建议"]


def test_digest_is_bounded_by_total_budget():
    """单条摘要必须有总量上限：否则 30 键 × 2KB 仍写 60KB/步。"""
    state = _big_state()
    for total in (4096, 2048, 1024):
        digest = payload.digest_state(state, total_budget=total)
        assert len(_dump(digest)) <= total, f"budget={total} 超限"
    # 默认预算下，摘要体积必须比原文小一个数量级
    raw = len(_dump(state))
    assert len(_dump(payload.digest_state(state))) < raw / 20


def test_digest_records_original_size_and_truncated_keys():
    """被裁掉的键名要能查：'主动截断' 可检测，不是静默丢数据。"""
    state = _big_state()
    digest = payload.digest_state(state)
    assert digest["_orig_chars"] == len(_dump(state))
    assert "response" in digest["_truncated"]
    assert digest["_clipped"]["response"]["chars"] == 2000


def test_digest_pathological_state_falls_back_to_structure_only():
    """键特别多的病态 state：退化为仅结构，仍有界、仍报原size。"""
    state = {f"k{i:03d}": "V" * 2000 for i in range(500)}
    digest = payload.digest_state(state)
    assert digest["_kind"] == "state_digest"
    assert digest["_note"]
    assert len(digest["_keys"]) <= payload.MAX_LISTED_KEYS
    assert digest["_keys_omitted"] == 500 - payload.MAX_LISTED_KEYS
    assert len(_dump(digest)) <= payload.TOTAL_BUDGET


def test_digest_passes_through_small_and_non_dict_values():
    assert payload.digest_state(None) is None
    assert payload.digest_state("短文本") == "短文本"
    assert payload.digest_state("X" * 5000) == "X" * payload.HEAD_CHARS
    assert payload.digest_state(42) == 42
    # 非 dict 的容器整体截断
    assert len(payload.digest_state(["a" * 9000])) == payload.HEAD_CHARS


def test_summary_detection_helpers():
    llm = _dump(payload.summarize_llm_prompt([["x"]]))
    state = _dump(payload.digest_state(_big_state()))
    assert payload.is_summary(llm) and payload.summary_kind(llm) == payload.LLM_PROMPT_KIND
    assert payload.is_summary(state) and payload.summary_kind(state) == payload.STATE_KIND
    assert not payload.is_summary('{"a": 1}')
    assert not payload.is_summary("普通文本")
    assert not payload.is_summary(None)
    # state_digest 不走还原（前端渲染摘要卡，见设计 §9.3 选项 A）
    assert payload.restore_step_text(state, []) == state

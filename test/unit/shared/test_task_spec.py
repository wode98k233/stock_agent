"""task_spec 提示词块。"""
import asyncio
import logging
from types import SimpleNamespace

from agents.shared.task_spec import (
    _clean_code,
    _clean_text,
    build_block,
    maybe_extract_task_spec,
    normalize_task_spec,
)


def test_build_block_contains_entities_and_rules():
    spec = {"entities": [
        {"key": "科技", "horizon": "1-5天", "position": "半仓以上",
         "candidates": [{"code": "588000", "name": "科创50ETF"},
                        {"code": "159516", "name": "半导体设备ETF"}]},
        {"key": "消费"},
    ], "global": {"focus": ["技术面", "资金流向"]}}
    block = build_block(spec)
    assert "【任务结构（来自用户确认）】" in block
    assert "科技" in block and "159516" in block
    assert "不得增删或合并" in block


def test_build_block_renders_entity_focus():
    """逐实体 focus 必须被消费（关注 技术面、资金流向），不能只生产不消费。"""
    spec = {"entities": [{"key": "科技", "focus": ["技术面", "资金流向"]}],
            "global": {"focus": ["技术面"]}}
    block = build_block(spec)
    assert "关注 技术面、资金流向" in block


def test_build_block_caps_global_focus():
    """global.focus 超过 MAX_FOCUS 时截断，与逐实体/候选上限保持一致。"""
    spec = {"entities": [{"key": "科技"}],
            "global": {"focus": [f"维度{i}" for i in range(8)]}}
    block = build_block(spec)
    assert "维度4" in block
    assert "维度5" not in block


def test_build_block_empty():
    assert build_block(None) == ""
    assert build_block({"entities": []}) == ""


def test_build_block_all_empty_keys_returns_empty():
    """实体 key 全为空时不得输出「只有标题+规则」的空块。"""
    assert build_block({"entities": [{"key": ""}, {"key": "   "}, {}]}) == ""


def _raw():
    return {"entities": [
        {"key": "科技", "horizon": "1-5天",
         "candidates": [{"code": "588000", "name": "科创50ETF"},
                        {"code": "159516", "name": "半导体设备ETF"}],
         "position": "半仓以上", "focus": ["技术面", "资金流向"]},
        {"key": "科技", "candidates": [{"name": "重复实体"}]},
        {"key": "消费", "candidates": [{"name": "消费ETF"}]},
    ], "global": {"focus": ["技术面"]}}


def test_normalize_dedup_and_caps():
    spec = normalize_task_spec(_raw())
    assert [e["key"] for e in spec["entities"]] == ["科技", "消费"]
    assert spec["entities"][0]["kind"] == "direction"
    assert spec["entities"][1]["candidates"][0]["code"] == ""


def test_normalize_empty_returns_none():
    assert normalize_task_spec({"entities": []}) is None
    assert normalize_task_spec(None) is None


def test_normalize_truncates_to_five(caplog):
    caplog.set_level(logging.INFO, logger="agents.shared.task_spec")
    raw = {"entities": [{"key": f"方向{i}"} for i in range(8)]}
    spec = normalize_task_spec(raw)
    assert len(spec["entities"]) == 5
    assert "task_spec.truncated" in caplog.text


# ── LLM 输出边界硬化 ────────────────────────────────────────

def test_normalize_non_list_entities_returns_none():
    """entities=5 不是可迭代容器：必须返回 None，而不是抛 TypeError。"""
    assert normalize_task_spec({"entities": 5}) is None
    assert normalize_task_spec({"entities": "科技"}) is None


def test_normalize_non_list_candidates_tolerated():
    """单实体 candidates=5：按 [] 处理，不得拖垮整份 spec。"""
    spec = normalize_task_spec({"entities": [{"key": "科技", "candidates": 5,
                                              "horizon": "1-5天"}]})
    assert spec["entities"][0]["candidates"] == []


def test_normalize_non_str_kind_tolerated():
    spec = normalize_task_spec({"entities": [{"key": "科技", "kind": ["symbol"]},
                                             {"key": "消费", "kind": 3}]})
    assert [e["kind"] for e in spec["entities"]] == ["direction", "direction"]


def test_normalize_focus_string_is_not_split_into_chars():
    """focus 写成字符串时按单元素列表处理，不能逐字符切成 ["技","术","面"]。"""
    spec = normalize_task_spec({
        "entities": [{"key": "科技", "focus": "技术面"}],
        "global": {"focus": "技术面"},
    })
    assert spec["entities"][0]["focus"] == ["技术面"]
    assert spec["global"]["focus"] == ["技术面"]


def test_normalize_sparse_entity_is_kept_and_logged(caplog):
    """只有 key 的实体：方向 key 本身是信息，必须保留，但要留 sparse 痕迹。"""
    caplog.set_level(logging.INFO, logger="agents.shared.task_spec")
    spec = normalize_task_spec({"entities": [{"key": "科技"}]})
    assert [e["key"] for e in spec["entities"]] == ["科技"]
    assert "task_spec.sparse" in caplog.text
    assert "key=科技" in caplog.text


# ── maybe_extract_task_spec 跳过条件 ────────────────────────

def _budget(max_calls: int, calls: int):
    return SimpleNamespace(
        limits=SimpleNamespace(max_llm_calls_per_query=max_calls),
        get_calls=lambda: calls,
    )


def test_maybe_extract_skips_without_llm():
    assert asyncio.run(maybe_extract_task_spec("问题", "回答", None)) is None


def test_maybe_extract_skips_empty_inputs():
    assert asyncio.run(maybe_extract_task_spec("", "回答", object())) is None
    assert asyncio.run(maybe_extract_task_spec("问题", "", object())) is None


def test_maybe_extract_skips_when_dashboard_disabled(monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "DASHBOARD_ENABLED", False)
    assert asyncio.run(maybe_extract_task_spec("问题", "回答", object())) is None


def test_maybe_extract_invalid_payload_returns_none(monkeypatch):
    """归一化抛异常也只能返回 None：抽取失败不得成为主链故障点。"""
    import utils.llm_factory as llm_factory
    monkeypatch.setattr(llm_factory, "llm_json_with_retry",
                        lambda *a, **kw: {"entities": 5})
    assert asyncio.run(maybe_extract_task_spec("问题", "回答", object())) is None


def test_maybe_extract_budget_gate(monkeypatch):
    """剩余 2 次不调 LLM；剩余 3 次才真正抽取。"""
    import utils.llm_factory as llm_factory

    calls = []

    def fake_llm(*args, **kwargs):
        calls.append(1)
        return {"entities": [{"key": "科技"}]}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", fake_llm)

    assert asyncio.run(maybe_extract_task_spec(
        "问题", "回答", object(), budget=_budget(10, 8))) is None
    assert calls == []

    spec = asyncio.run(maybe_extract_task_spec(
        "问题", "回答", object(), budget=_budget(10, 7)))
    assert calls == [1]
    assert spec["entities"][0]["key"] == "科技"


def test_maybe_extract_marks_cache_and_attribution(monkeypatch):
    """锁：抽取调用必须显式关闭语义缓存并带 purpose/label 归因（与 clarify 同口径）。"""
    import utils.llm_factory as llm_factory

    captured = {}

    def fake_llm(*args, **kwargs):
        captured.update(kwargs)
        return {"entities": [{"key": "科技"}]}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", fake_llm)
    spec = asyncio.run(maybe_extract_task_spec(
        "问题", "回答", object(), metadata={"node": "classifier"}))

    assert spec["entities"][0]["key"] == "科技"
    meta = captured["metadata"]
    assert meta["purpose"] == "task_spec"
    assert meta["label"] == "任务结构抽取"
    assert meta["sr_cacheable"] is False
    # 调用方 metadata 仍要透传（node 等归因键不能被覆盖丢掉）
    assert meta["node"] == "classifier"


# ── 归一化边界：代码与文本 ────────────────────────────────

def test_clean_code_accepts_exact_six_digits_only():
    """锁：只接受恰好 6 位裸码；7 位数字串不得被 [-6:] 静默截断。"""
    assert _clean_code("sh588000") == "588000"
    assert _clean_code("588000.SH") == "588000"
    assert _clean_code("600519") == "600519"
    assert _clean_code("1595160") == ""
    assert _clean_code("159516.0") == ""


def test_clean_text_drops_containers_and_bool():
    """dict/list 不得转 repr 混进契约；int/float 仍转字符串，bool→空串。"""
    assert _clean_text({"a": 1}, 20) == ""
    assert _clean_text([1, 2], 20) == ""
    assert _clean_text(True, 20) == ""
    assert _clean_text(None, 20) == ""
    assert _clean_text(5, 20) == "5"
    assert _clean_text(15.9516, 20) == "15.9516"


def test_normalize_drops_container_horizon_and_position():
    spec = normalize_task_spec({"entities": [
        {"key": "科技", "horizon": {"value": 5}, "position": ["半仓"]},
    ]})
    assert spec["entities"][0]["horizon"] == ""
    assert spec["entities"][0]["position"] == ""
    assert spec["entities"][0]["candidates"] == []


def test_normalize_sparse_with_focus_not_flagged(caplog):
    """带 focus 的实体不是 sparse：focus 是有效信息，不得刷 sparse 日志。"""
    caplog.set_level(logging.INFO, logger="agents.shared.task_spec")
    spec = normalize_task_spec({"entities": [{"key": "科技", "focus": ["技术面"]}]})
    assert spec["entities"][0]["focus"] == ["技术面"]
    assert "task_spec.sparse" not in caplog.text

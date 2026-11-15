"""仪表盘多实体契约：schema、归一化、后处理与两阶段接线。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from agents.analysis import dashboard_config
from agents.analysis import dashboard_generator as dg
from agents.analysis.dashboard_config import get_dashboard_def, validate_dashboards_llm_fields
from agents.analysis.dashboard_generator import (
    _as_text,
    _build_prompt,
    _derive_decision_type,
    _normalize_llm_payload,
    _validate_groups,
)
from agents.analysis.dashboard_schema import DashboardData, DashboardEntry, DashboardGroup
from config import Config


def _base(**kw):
    data = dict(core_verdict="三方向", decision_type="buy", confidence_level=0.62,
                sentiment_score=68, trend_prediction="bullish")
    data.update(kw)
    return data


def _group(**kw):
    g = dict(key="科技", decision="buy", signal_symbol="159516",
             horizon="1-5天", stance="可参与但只低吸",
             entry={"type": "pullback", "low": 0.46, "high": 0.47})
    g.update(kw)
    return g


# ── schema ──────────────────────────────────────────────────

def test_group_roundtrip():
    data = DashboardData(**_base(groups=[_group()]))
    assert data.groups[0].key == "科技"
    assert data.groups[0].entry.type == "pullback"
    assert data.groups[0].decision == "buy"


def test_group_defaults():
    g = DashboardGroup(key="消费", decision="hold")
    assert g.kind == "direction"
    assert g.entry.type == "none"
    assert g.candidates == []


def test_invalid_group_decision_rejected():
    with pytest.raises(ValidationError):
        DashboardGroup(key="x", decision="strong_buy")


def test_invalid_entry_type_rejected():
    with pytest.raises(ValidationError):
        DashboardEntry(type="limit_order")


# ── 整体口径推导 ────────────────────────────────────────────

def _groups_with_decisions(*decisions):
    return [DashboardGroup(key=f"g{i}", decision=d) for i, d in enumerate(decisions)]


def test_derive_decision_type_majority():
    assert _derive_decision_type(_three_groups()) == "buy"
    assert _derive_decision_type([DashboardGroup(key="a", decision="hold")]) == "hold"


def test_derive_decision_type_boundaries():
    # 平局（1:1、2:2）与全 hold 都必须是 hold，严格多数才给方向
    assert _derive_decision_type(_groups_with_decisions("buy", "sell")) == "hold"
    assert _derive_decision_type(
        _groups_with_decisions("buy", "buy", "sell", "sell")) == "hold"
    assert _derive_decision_type(_groups_with_decisions("hold", "hold")) == "hold"
    assert _derive_decision_type(
        _groups_with_decisions("buy", "buy", "buy", "sell", "sell")) == "buy"
    assert _derive_decision_type([]) == "hold"


# ── LLM 输出归一化（组级容错，不拖垮整张仪表盘）─────────────

def test_normalize_groups_tolerates_nulls_and_drops_bad_decision():
    raw = _base(groups=[
        # entry/horizon/candidates/confidence 全不合法：应逐项降级
        {"key": "科技", "decision": "buy", "horizon": None, "stance": None,
         "position": None, "invalidation": None, "note": None, "signal_name": None,
         "signal_symbol": None, "candidates": None, "entry": None,
         "confidence": "high"},
        # 缺 decision：整组丢弃（组级容错，不让整盘 fallback）
        {"key": "消费", "horizon": None, "candidates": None, "entry": None},
        # 非法 decision：同样丢弃
        {"key": "红利", "decision": "maybe"},
        # 非 dict：跳过
        "oops",
        # 非法 entry.type + 非数值 low/high + 候选字段 None：降级为 unverifiable
        {"key": "医疗", "decision": "hold", "signal_symbol": " 513060 ",
         "candidates": [{"code": None, "name": None, "note": None}, "oops"],
         "entry": {"type": "wait", "low": "0.5", "high": None}},
    ])

    out = _normalize_llm_payload(raw)
    groups = out["groups"]

    assert [g["key"] for g in groups] == ["科技", "医疗"]

    tech = groups[0]
    assert tech["horizon"] == "" and tech["stance"] == ""
    assert tech["position"] == "" and tech["invalidation"] == ""
    assert tech["note"] == "" and tech["signal_name"] == ""
    assert tech["signal_symbol"] == ""
    assert tech["candidates"] == []
    assert tech["entry"] == {"type": "unverifiable", "low": None, "high": None, "note": ""}
    assert tech["confidence"] is None

    med = groups[1]
    assert med["entry"]["type"] == "unverifiable"
    assert med["entry"]["low"] is None and med["entry"]["high"] is None
    assert med["candidates"] == [{"code": "", "name": "", "note": ""}]
    assert med["signal_symbol"] == "513060"

    # 归一化后的 payload 必须能直接过 schema（不再整盘降级）
    data = DashboardData.model_validate(out)
    assert data.groups[0].entry.type == "unverifiable"
    assert data.groups[1].signal_symbol == "513060"


def test_normalize_groups_keeps_empty_list_written_back():
    out = _normalize_llm_payload(_base(groups=[None, {"key": ""}, {"key": "x"}]))
    assert out["groups"] == []

    untouched = _normalize_llm_payload(_base())
    assert "groups" not in untouched


def test_as_text_coerces_numbers_and_drops_non_text():
    # LLM 常把 6 位代码/周期写成数字；bool 必须排在最前（它是 int 子类）
    assert _as_text(None) == ""
    assert _as_text("1-5天") == "1-5天"
    assert _as_text(True) == "" and _as_text(False) == ""
    assert _as_text(159516) == "159516"
    assert _as_text(5) == "5"
    assert _as_text(0.46) == "0.46"
    assert _as_text([1, 2]) == ""
    assert _as_text({"a": 1}) == ""


def test_normalize_groups_coerces_numeric_text_fields():
    """数值型文本字段（代码/周期等）必须转成字符串，否则整盘 DashboardData 校验失败→fallback。"""
    raw = _base(groups=[{
        "key": "科技", "decision": "buy",
        "horizon": 5, "stance": 3,
        "signal_symbol": 159516,
        "candidates": [{"code": 159516, "name": "半导体设备ETF", "note": 5}],
        "entry": {"type": "none", "note": 5},
    }])

    out = _normalize_llm_payload(raw)
    group = out["groups"][0]
    assert group["horizon"] == "5"
    assert group["stance"] == "3"
    assert group["signal_symbol"] == "159516"
    assert group["candidates"][0]["code"] == "159516"
    assert group["candidates"][0]["note"] == "5"
    assert group["entry"]["note"] == "5"

    data = DashboardData.model_validate(out)
    assert data.groups[0].horizon == "5"
    assert data.groups[0].stance == "3"
    assert data.groups[0].signal_symbol == "159516"
    assert data.groups[0].candidates[0].code == "159516"


def test_normalize_groups_unhashable_enum_values_downgrade():
    """kind=[] / entry.type={} 不可哈希：枚举判断不得抛 TypeError，非法值降级而非整盘 fallback。"""
    out = _normalize_llm_payload(_base(groups=[{
        "key": "科技", "decision": "buy", "kind": [],
        "entry": {"type": {}, "low": 0.46},
        "signal_symbol": {},
        "candidates": [{"code": [], "name": {}, "note": []}],
    }]))

    group = out["groups"][0]
    assert group["kind"] == "direction"          # 非法 kind → 默认 direction
    assert group["entry"]["type"] == "unverifiable"
    assert group["signal_symbol"] == ""
    assert group["candidates"] == [{"code": "", "name": "", "note": ""}]

    DashboardData.model_validate(out)


# ── 后处理 ──────────────────────────────────────────────────

class _FakeLogger:
    """radar logger 的 info(tag, msg, **kw) 形状；标准 logging 不能直接冒充。"""

    def __init__(self):
        self.events = []

    def info(self, tag, msg, **kw):
        self.events.append((tag, msg, kw))


def _three_groups():
    return [
        DashboardGroup(key="科技", decision="buy", signal_symbol="159516",
                       entry=DashboardEntry(type="pullback", low=0.46, high=0.47)),
        DashboardGroup(key="消费", decision="hold", signal_symbol="159928"),
        DashboardGroup(key="医疗", decision="buy", signal_symbol="513060",
                       entry=DashboardEntry(type="pullback", high=0.50)),
    ]


def test_validate_drops_symbol_not_in_report():
    log = _FakeLogger()
    data = DashboardData(**_base(groups=_three_groups()))
    _validate_groups(data, "报告里只提到 159928 和 513060", None, log)
    assert data.groups[0].signal_symbol == ""
    assert data.groups[1].signal_symbol == "159928"
    assert any(m == "dashboard.gen.signal_symbol_drop" for _, m, _ in log.events)


def test_validate_signal_symbol_requires_numeric_boundary():
    def _check(report, symbol="159516"):
        g = DashboardGroup(key="科技", decision="buy", signal_symbol=symbol)
        data = DashboardData(**_base(groups=[g]))
        _validate_groups(data, report, None, _FakeLogger())
        return data.groups[0].signal_symbol

    # 前后缀（sh / .SH）仍应通过；更长数字串（1595160）不得误放
    assert _check("sh159516") == "159516"
    assert _check("159516.SH") == "159516"
    assert _check("159516") == "159516"
    assert _check("1595160") == ""
    assert _check("1159516") == ""


def test_validate_strips_valid_signal_symbol():
    g = DashboardGroup(key="科技", decision="buy", signal_symbol=" 159516 ")
    data = DashboardData(**_base(groups=[g]))
    _validate_groups(data, "159516", None, _FakeLogger())
    assert data.groups[0].signal_symbol == "159516"


def test_validate_downgrades_entry_without_bounds():
    log = _FakeLogger()
    g = DashboardGroup(key="科技", decision="buy", signal_symbol="159516",
                       entry=DashboardEntry(type="breakout"))
    data = DashboardData(**_base(groups=[g]))
    _validate_groups(data, "159516", None, log)
    assert data.groups[0].entry.type == "unverifiable"
    assert any(m == "dashboard.gen.entry_unverifiable" for _, m, _ in log.events)


def test_validate_overrides_decision_type():
    data = DashboardData(**_base(groups=_three_groups(), decision_type="sell"))
    _validate_groups(data, "159516 159928 513060", None, _FakeLogger(),
                     derive_decision=True)
    assert data.decision_type == "buy"


def test_validate_keeps_decision_by_default():
    """derive_decision 默认 False：只有显式 true 才按组覆盖整体判断。"""
    data = DashboardData(**_base(groups=_three_groups(), decision_type="sell"))
    _validate_groups(data, "159516 159928 513060", None, _FakeLogger())
    assert data.decision_type == "sell"


def test_validate_groups_keeps_decision_when_not_deriving():
    """未声明 derive 的仪表盘：groups 只是明细，不得用组内多数票改写整体判断。"""
    log = _FakeLogger()
    data = DashboardData(**_base(groups=_three_groups(), decision_type="sell"))
    _validate_groups(data, "159516 159928 513060", None, log, derive_decision=False)
    assert data.decision_type == "sell"
    kept = [kw for _, m, kw in log.events if m == "dashboard.gen.groups_kept"]
    assert kept and kept[0]["llm"] == "sell" and kept[0]["groups"] == 3


def test_validate_logs_spec_mismatch():
    spec = {"entities": [{"key": "科技"}, {"key": "消费"}, {"key": "医疗"}, {"key": "红利"}]}
    log = _FakeLogger()
    data = DashboardData(**_base(groups=_three_groups()))
    _validate_groups(data, "159516 159928 513060", spec, log)
    assert any(m == "dashboard.gen.groups_mismatch" for _, m, _ in log.events)


def test_validate_logs_spec_mismatch_even_without_groups():
    log = _FakeLogger()
    data = DashboardData(**_base(decision_type="sell", groups=[]))
    _validate_groups(data, "159516", {"entities": [{"key": "科技"}]}, log)
    mismatch = [kw for _, m, kw in log.events if m == "dashboard.gen.groups_mismatch"]
    assert mismatch and mismatch[0]["got"] == []
    # 空 groups 不覆盖 LLM 的 decision_type
    assert data.decision_type == "sell"


# ── 提示词 ──────────────────────────────────────────────────

def test_prompt_contains_task_spec_block():
    spec = {"entities": [{"key": "科技", "horizon": "1-5天",
                          "candidates": [{"code": "159516", "name": "半导体设备ETF"}]}],
            "global": {"focus": ["技术面", "资金流向"]}}
    msgs = _build_prompt("报告正文", {}, "问题", scenario_tag="板块决策仪表盘",
                         dashboard_id="sector", task_spec=spec)
    user = msgs[-1][1]
    assert "【任务结构（来自用户确认）】" in user
    assert "159516" in user and "科技" in user
    assert "不得增删或合并" in user


def test_prompt_without_spec_has_no_block():
    msgs = _build_prompt("报告正文", {}, "问题", scenario_tag="板块决策仪表盘",
                         dashboard_id="sector")
    assert "【任务结构" not in msgs[-1][1]


def test_prompt_groups_hint_for_all_dashboards():
    """groups 已通用化：任何模板都可能一次问多个方向，提示不再按 llm_field 声明。"""
    for did, tag in (("sector", "板块决策仪表盘"),
                     ("stock", "个股决策仪表盘"),
                     ("market", "市场决策仪表盘")):
        msgs = _build_prompt("报告正文", {}, "问题", scenario_tag=tag,
                             dashboard_id=did)
        user = msgs[-1][1]
        assert "注意（groups）" in user, did
        # 提示必须落在 schema 的 } 之后（task_head 末尾），不是 system
        assert "}\n注意（groups）" in user
        assert "注意（groups）" not in msgs[0][1]


def test_prompt_forks_conversation_with_task_spec():
    spec = {"entities": [{"key": "科技"}]}
    conversation = [("system", "S"), ("user", "U")]
    msgs = _build_prompt("报告正文", {}, "问题", scenario_tag="板块决策仪表盘",
                         dashboard_id="sector", conversation=conversation,
                         report_fork=True, task_spec=spec)
    assert msgs[:2] == conversation
    assert msgs[-1][0] == "user"
    assert "【任务结构（来自用户确认）】" in msgs[-1][1]


def test_prompt_dedupes_groups_declared_as_llm_field(monkeypatch):
    """自定义/导入模板若把 groups 声明成 llm_field，通用 schema 里不得出现第二个 "groups":。"""
    defn = {
        "id": "custom",
        "display_name": "自定义仪表盘",
        "sections": [{"id": "core_verdict", "renderer": "verdict_text",
                      "fields": ["core_verdict"]}],
        "llm_fields": [
            {"name": "groups", "type": "array"},
            {"name": "custom_note", "type": "str", "example": '"自定义说明"'},
        ],
    }
    monkeypatch.setattr(dashboard_config, "get_dashboard_def", lambda _did: defn)
    user = _build_prompt("报告", {}, "q", dashboard_id="custom")[-1][1]
    assert user.count('"groups":') == 1
    # 只去重 groups，其他扩展字段照常保留
    assert '"custom_note": "自定义说明"' in user


# ── 两阶段 generate 接线 ────────────────────────────────────

def _full_dashboard_with_one_group():
    return {
        "core_verdict": "科技方向可低吸",
        "decision_type": "sell",          # 与 groups 矛盾，应由推导覆盖为 buy
        "confidence_level": 0.66,
        "sentiment_score": 62,
        "trend_prediction": "bullish",
        "quality_tag": "完整分析",
        "checklist": [],
        "risk_priority": [],
        "key_points": ["半导体设备ETF回踩低吸"],
        "next_watch": [],
        "groups": [{
            "key": "科技",
            "kind": "direction",
            "horizon": "1-5天",
            "decision": "buy",
            "confidence": 0.66,
            "stance": "只低吸",
            "candidates": [{"code": "159516", "name": "半导体设备ETF"}],
            "signal_symbol": "159516",
            "signal_name": "半导体设备ETF",
            "position": "≤10%",
            "entry": {"type": "pullback", "low": 0.46, "high": 0.47,
                      "note": "回踩0.46-0.47"},
            "invalidation": "破0.456",
        }],
    }


def test_generate_two_phase_passes_task_spec_and_uses_derived_metadata():
    """锁：两阶段都透传 task_spec；metadata 用推导后的 decision_type（问题 3）。"""
    spec = {"entities": [{"key": "科技",
                          "candidates": [{"code": "159516", "name": "半导体设备ETF"}]}]}
    calls = []
    meta_args = []
    real_build_prompt = dg._build_prompt

    async def fake_call(messages, budget, logger_obj, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {"need_tools": ["mx"], "reason": "缺量能数据"}
        return _full_dashboard_with_one_group()

    def record_meta(raw):
        meta_args.append(raw)
        return {}

    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch.object(dg, "_call_llm", new=AsyncMock(side_effect=fake_call)), \
         patch.object(dg, "_build_prompt", side_effect=real_build_prompt) as mock_prompt, \
         patch.object(dg, "extract_dashboard_metadata", side_effect=record_meta), \
         patch.object(dg, "set_dashboard_meta"):
        data = asyncio.run(dg.generate(
            report_content="科技方向：159516 半导体设备ETF 回踩 0.46-0.47 可低吸。",
            slot_results={"mx": "涨幅3.5%，量能放大"},
            template_id="sector_timing",
            user_input="科技方向现在能买吗",
            dashboard_id="sector",
            task_spec=spec,
        ))

    assert data is not None
    # 单组 buy 即严格多数（1/1），推导覆盖 LLM 的 sell；只有平局（如 buy+sell）才推 hold
    assert data.decision_type == "buy"
    assert data.groups[0].signal_symbol == "159516"
    # 两阶段各调一次 _build_prompt，且第二次也带同一 task_spec（透传没断）
    assert mock_prompt.call_count == 2
    assert [c.kwargs.get("task_spec") for c in mock_prompt.call_args_list] == [spec, spec]
    # metadata 必须拿推导后的 decision_type，否则记忆 sentiment 与展示相反
    assert meta_args and meta_args[0]["decision_type"] == "buy"


def test_generate_passes_spec_for_all_dashboards():
    """task_spec 不再按 dashboard 能力闸门拦截：任何仪表盘都做实体对齐。

    是否用组内多数覆盖顶层 decision 由 derive_decision_from_groups 决定：
    sector=true（推导），stock/market 未声明 → 保留整体判断。
    """
    spec = {"entities": [{"key": "科技"}, {"key": "消费"}]}
    seen = []
    real_validate = dg._validate_groups

    def spy(data, report_text, task_spec, logger, derive_decision=True):
        seen.append((task_spec, derive_decision))
        return real_validate(data, report_text, task_spec, logger,
                             derive_decision=derive_decision)

    def _run(dashboard_id: str, template_id: str):
        payload = _base(groups=[], decision_type="sell")
        with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
             patch.object(dg, "_call_llm", new=AsyncMock(return_value=payload)), \
             patch.object(dg, "extract_dashboard_metadata", return_value={}), \
             patch.object(dg, "set_dashboard_meta"), \
             patch.object(dg, "_validate_groups", side_effect=spy):
            return asyncio.run(dg.generate(
                report_content="报告正文", slot_results={}, template_id=template_id,
                user_input="问题", dashboard_id=dashboard_id, task_spec=spec))

    assert _run("stock", "stock_deep_dive") is not None
    assert _run("market", "market_daily") is not None
    assert _run("sector", "sector_landscape") is not None
    assert seen == [(spec, False), (spec, False), (spec, True)]


def test_generate_market_keeps_groups_and_llm_decision():
    """市场仪表盘没有 derive 开关：groups 保留为多方向明细，整体判断不被改写。

    场景：大盘整体偏弱（sell），局部科技方向可低吸（buy）—— 这是多方向问题，
    但整体口径是市场结论，不能被单组多数票翻成 buy。
    """
    spec = {"entities": [{"key": "科技"}]}
    payload = _base(groups=[_group()], decision_type="sell")
    with patch.object(Config, "DASHBOARD_LLM_ENABLED", True), \
         patch.object(dg, "_call_llm", new=AsyncMock(return_value=payload)), \
         patch.object(dg, "extract_dashboard_metadata", return_value={}), \
         patch.object(dg, "set_dashboard_meta"):
        data = asyncio.run(dg.generate(
            report_content="科技方向：159516 半导体设备ETF 回踩 0.46 可低吸，但大盘整体偏弱。",
            slot_results={}, template_id="market_daily",
            user_input="科技方向和大盘怎么看", dashboard_id="market",
            task_spec=spec))

    assert data is not None
    assert data.decision_type == "sell"              # LLM 整体判断保留
    assert [g.key for g in data.groups] == ["科技"]  # 组级明细不被清空
    assert data.groups[0].signal_symbol == "159516"


# ── dashboard 定义 ──────────────────────────────────────────

def test_sector_declares_groups_section_and_derive_flag():
    d = get_dashboard_def("sector")
    assert any(s.get("renderer") == "group_cards" for s in d["sections"])
    assert d.get("derive_decision_from_groups") is True
    # groups 已进通用 schema：sector 不再用 llm_field 重复声明，避免两处定义漂移
    assert not any(f["name"] == "groups" for f in d["llm_fields"])


def test_market_dashboard_has_derive_flag_false():
    d = get_dashboard_def("market")
    assert d.get("derive_decision_from_groups") is False


def test_prompt_groups_schema_covers_all_decisions_and_entry_types():
    """通用 schema 的 groups 示例 + 提示必须覆盖三种 decision 与四种 entry.type。"""
    user = _build_prompt("报告", {}, "q", dashboard_id="stock")[-1][1]
    assert '"groups": [{"key":"科技"' in user
    assert '"decision":"buy"' in user
    assert '"entry":{"type":"pullback"' in user
    assert "buy/hold/sell" in user
    assert "none/pullback/breakout/unverifiable" in user


def test_all_dashboard_llm_fields_whitelisted():
    assert validate_dashboards_llm_fields() == []


def test_prompt_injects_spec_for_all_dashboards():
    spec = {"entities": [{"key": "科技"}]}
    for did, tag in (("sector", "板块决策仪表盘"),
                     ("stock", "个股决策仪表盘"),
                     ("market", "市场决策仪表盘")):
        user = _build_prompt("报告", {}, "q", scenario_tag=tag,
                             dashboard_id=did, task_spec=spec)[-1][1]
        assert "【任务结构（来自用户确认）】" in user, did


def test_prompt_stock_without_groups_field_still_gets_groups_schema():
    """stock 没有 groups llm_field，也必须拿到通用 schema —— 多方向问题不挑模板。"""
    assert not any(f["name"] == "groups" for f in get_dashboard_def("stock")["llm_fields"])
    user = _build_prompt("报告", {}, "q", dashboard_id="stock")[-1][1]
    assert '"groups": [{"key":"科技"' in user
    assert "仅当用户一次问多个方向/标的组时输出" in user

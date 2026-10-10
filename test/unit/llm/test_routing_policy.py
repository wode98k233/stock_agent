"""A8 模型路由 policy 单测（全部 mock，不真实调 LLM）。

覆盖 PRD-A8 §8：AC-5（fast/strong 档路由决策 + trace model_tier 字段）、
FR-05（映射表热改配置生效）、FR-07（开关关闭回退）。
"""
import asyncio
import json
import logging

import pytest

import importlib

gateway_mod = importlib.import_module("utils.llm.gateway")
from config import Config
from utils.llm.gateway import LLMGateway
from utils.llm.policies.routing_policy import ModelRoutingPolicy
from utils.llm.policies.cache_policy import ResponseCachePolicy, get_shared_stats
from utils.llm.registry import ModelRegistry

from agents.sr_context import reset_sr_context


@pytest.fixture(autouse=True)
def _clean_sr_channel():
    """隔离 agents 侧 run 级 sr_* 通道（gateway._sr_context 会合并它）。"""
    reset_sr_context()
    yield
    reset_sr_context()


MAP = {"rules": [
    {"match": {"scenario": ["market_quick", "data_query"]}, "tier": "fast"},
    {"match": {"label": ["report_enhance", "deep_research", "debate"]}, "tier": "strong"},
    {"match": {"purpose": ["compress", "judge"]}, "tier": "fast"},
], "default": "standard"}


def make_policy(*, routing_map=None, tier_models=None, enabled=True):
    get_shared_stats().reset()
    return ModelRoutingPolicy(routing_map=routing_map, tier_models=tier_models, enabled=enabled)


# ── 基本映射（AC-5） ──

def test_scenario_fast():
    rp = make_policy(routing_map=MAP)
    d = rp.resolve(purpose="agent", label="agent", metadata={"sr_scenario": "market_quick"})
    assert d.tier == "fast"
    assert d.reason.startswith("rule#")


def test_label_strong():
    rp = make_policy(routing_map=MAP)
    d = rp.resolve(purpose="report", label="report_enhance", metadata={})
    assert d.tier == "strong"


def test_purpose_fast():
    rp = make_policy(routing_map=MAP)
    d = rp.resolve(purpose="compress", label="compress", metadata={})
    assert d.tier == "fast"


def test_default_standard_when_no_match():
    rp = make_policy(routing_map=MAP)
    d = rp.resolve(purpose="agent", label="agent", metadata={"sr_scenario": "stock_analysis"})
    assert d.tier == "standard"
    assert d.reason == "default"


def test_missing_metadata_falls_back_default():
    rp = make_policy(routing_map=MAP)
    assert rp.resolve(purpose="agent", label="agent", metadata=None).tier == "standard"
    assert rp.resolve(purpose="agent", label="agent", metadata={}).tier == "standard"


def test_first_match_wins():
    """规则按顺序首条命中生效。"""
    rp = make_policy(routing_map={"rules": [
        {"match": {"label": ["report_enhance"]}, "tier": "strong"},
        {"match": {"label": ["report_enhance"], "purpose": ["report"]}, "tier": "fast"},
    ], "default": "standard"})
    d = rp.resolve(purpose="report", label="report_enhance", metadata={})
    assert d.tier == "strong"  # 第 1 条先命中，不会走到第 2 条


def test_multi_dimension_match_is_and():
    """同一规则多维度为「与」关系：维度齐全才命中。"""
    rp = make_policy(routing_map={"rules": [
        {"match": {"label": ["report_enhance"], "purpose": ["agent"]}, "tier": "strong"},
    ], "default": "standard"})
    assert rp.resolve(purpose="report", label="report_enhance", metadata={}).tier == "standard"
    assert rp.resolve(purpose="agent", label="report_enhance", metadata={}).tier == "strong"


# ── 档→模型绑定 ──

def test_tier_model_binding():
    rp = make_policy(routing_map=MAP, tier_models={"fast": "flash-model"})
    d = rp.resolve(purpose="agent", label="agent", metadata={"sr_scenario": "market_quick"})
    assert d.tier == "fast"
    assert d.model_override == "flash-model"


def test_unbound_tier_keeps_purpose_model():
    """档位未绑定模型（或空串）→ model_override=None（沿用 purpose 原配）。"""
    rp = make_policy(routing_map=MAP, tier_models={"fast": ""})
    d = rp.resolve(purpose="agent", label="agent", metadata={"sr_scenario": "market_quick"})
    assert d.tier == "fast"
    assert d.model_override is None


def test_invalid_tier_normalized_to_standard():
    rp = make_policy(routing_map={"rules": [{"match": {"label": ["x"]}, "tier": "turbo"}],
                                   "default": "standard"})
    d = rp.resolve(purpose="agent", label="x", metadata={})
    assert d.tier == "standard"


# ── 开关回退（FR-07 / AC-6） ──

def test_disabled_returns_standard_no_override():
    rp = make_policy(enabled=False)
    d = rp.resolve(purpose="agent", label="report_enhance",
                   metadata={"sr_scenario": "market_quick"})
    assert d.tier == "standard"
    assert d.model_override is None
    assert d.reason == "routing_disabled"


# ── 热改配置生效（FR-05） ──

def test_hot_reload_mapping_from_config(monkeypatch):
    """routing_map=None 时每次 resolve 现读 Config → 热改配置立即生效。"""
    rp = make_policy(enabled=True)  # 走 Config 默认表
    assert rp.resolve(purpose="agent", label="agent",
                      metadata={"sr_scenario": "market_quick"}).tier == "fast"
    new_map = {"rules": [{"match": {"label": ["zzz"]}, "tier": "strong"}], "default": "fast"}
    monkeypatch.setattr(Config, "LLM_ROUTING_MAP", json.dumps(new_map))
    assert rp.resolve(purpose="agent", label="other", metadata={}).tier == "fast"
    assert rp.resolve(purpose="agent", label="zzz", metadata={}).tier == "strong"


def test_hot_reload_tier_models_from_config(monkeypatch):
    monkeypatch.setattr(Config, "LLM_ROUTING_ENABLED", True)
    monkeypatch.setattr(Config, "LLM_TIER_FAST_MODEL", "flash-from-config")
    rp = make_policy()  # 全部走 Config
    d = rp.resolve(purpose="agent", label="agent", metadata={"sr_scenario": "market_quick"})
    assert d.tier == "fast"
    assert d.model_override == "flash-from-config"


# ── 计量 ──

def test_record_tier_shared_stats():
    rp = make_policy(routing_map=MAP)
    rp.resolve(purpose="agent", label="agent", metadata={"sr_scenario": "market_quick"})
    rp.resolve(purpose="report", label="report_enhance", metadata={})
    routing = get_shared_stats().snapshot()["routing"]
    assert routing.get("fast") == 1
    assert routing.get("strong") == 1


# ── Gateway 集成：路由 override 生效 + trace 计量字段存在（AC-5，mock 计数） ──

class FakeMsg:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, model):
        self.model_name = model
        self.calls = 0
        self.last_config = None

    def invoke(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        return FakeMsg("ok-from-" + self.model_name)

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        return FakeMsg("ok-from-" + self.model_name)

    def stream(self, messages, config=None):
        self.calls += 1
        self.last_config = config

        def gen():
            yield FakeMsg("c1-" + self.model_name)
            yield FakeMsg("c2-" + self.model_name)

        return gen()

    async def astream(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        yield FakeMsg("c1-" + self.model_name)
        yield FakeMsg("c2-" + self.model_name)


def _make_gateway(monkeypatch, routing_policy):
    created = []

    def _build(profile):
        m = FakeModel(profile.model)
        created.append(m)
        return m

    monkeypatch.setattr(gateway_mod, "build_chat_model", _build)
    gw = LLMGateway(registry=ModelRegistry(yaml_path=None))
    gw._cache_policy_inst = ResponseCachePolicy(db_path=":memory:", enabled=False)
    gw._routing_policy_inst = routing_policy
    gw._created_models = created
    return gw


def test_gateway_routing_model_override_and_trace_fields(monkeypatch):
    """场景类查询走 fast 档模型；metadata 注入 model_tier/cache_hit（AC-5 trace 落点）。"""
    rp = make_policy(routing_map=MAP, tier_models={"fast": "flash-model"})
    gw = _make_gateway(monkeypatch, rp)
    r = gw.invoke("agent", [("user", "今天大盘怎么看")], logging.getLogger("t"),
                  label="agent", metadata={"sr_scenario": "market_quick"})
    assert r.content == "ok-from-flash-model"
    md = (gw._created_models[0].last_config or {}).get("metadata") or {}
    assert md.get("model_tier") == "fast"
    assert md.get("cache_hit") is False
    assert get_shared_stats().snapshot()["routing"].get("fast") == 1


def test_gateway_routing_disabled_baseline(monkeypatch):
    """路由关闭：不换模型、不注入计量字段（行为与上线前一致）。"""
    gw = _make_gateway(monkeypatch, make_policy(enabled=False))
    r = gw.invoke("agent", [("user", "hi")], logging.getLogger("t"),
                  label="agent", metadata={"sr_scenario": "market_quick"})
    md = (gw._created_models[0].last_config or {}).get("metadata") or {}
    assert not r.content.startswith("ok-from-flash")
    assert "model_tier" not in md


def test_gateway_async_routing(monkeypatch):
    rp = make_policy(routing_map=MAP, tier_models={"strong": "beefy-model"})
    gw = _make_gateway(monkeypatch, rp)

    async def run():
        return await gw.ainvoke("report", [("user", "深度研究牧原")],
                                logging.getLogger("t"), label="deep_research",
                                metadata={})

    r = asyncio.run(run())
    assert r.content == "ok-from-beefy-model"


def test_gateway_stream_routing_applies(monkeypatch):
    """流式链只接路由（一期不接缓存）：default 档绑定的模型对流式同样生效。"""
    rp = make_policy(routing_map={"rules": [], "default": "standard"},
                     tier_models={"standard": "stream-model"})
    gw = _make_gateway(monkeypatch, rp)
    chunks = list(gw.stream("agent", [("user", "hi")]))
    assert [c.content for c in chunks] == ["c1-stream-model", "c2-stream-model"]
    assert get_shared_stats().snapshot()["routing"].get("standard") == 1

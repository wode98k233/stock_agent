"""A8 语义缓存 policy 单测（全部 mock / 临时库，不真实调 LLM）。

覆盖 PRD-A8 §8 AC 落点：
- AC-1 命中短路零 LLM 调用（gateway 集成 + mock 计数）
- AC-2 盘中/盘后互不命中（phase 入键 + 盘中禁缓存）
- AC-3 有/无注入互不命中（inject_hash 入键）
- AC-4 跨日失效（created_trade_date 守卫）
- AC-6 开关关闭回退（policy 级 + gateway 级基线一致）
- AC-8 键归一化各形态 / 注入哈希变化 → key 变化
"""
import logging
import sqlite3
from datetime import date

import pytest

# 注意：utils/llm/__init__.py 导出的 gateway 单例会遮蔽子模块，用 importlib 取模块对象
import importlib

gateway_mod = importlib.import_module("utils.llm.gateway")
from utils.llm.gateway import LLMGateway
from utils.llm.registry import ModelRegistry
from utils.llm.policies.cache_policy import (
    KEY_PREFIX,
    CacheEntry,
    ResponseCachePolicy,
    get_shared_stats,
    normalize_query,
)
from utils.llm.policies.routing_policy import ModelRoutingPolicy

from agents.sr_context import reset_sr_context


@pytest.fixture(autouse=True)
def _clean_sr_channel():
    """隔离 agents 侧 run 级 sr_* 通道，防止其他用例的 ContextVar 泄漏进网关。"""
    reset_sr_context()
    yield
    reset_sr_context()


TTL_MAP = {"market": 1800, "sector_intraday": 0, "sector_offhours": 14400,
           "methodology": 604800, "data_query": 0}
CATEGORY_MAP = {"market_quick": "market", "sector_analysis": "sector",
                "methodology": "methodology", "data_query": "data_query",
                "default": "sector"}
GOOD_META = {"sr_scenario": "market_quick", "sr_template_id": "tpl1",
             "sr_inject_hash": "abc123", "sr_cacheable": True}


# ── 构造辅助 ──

def make_policy(tmp_path, *, enabled=True, phase_offhours=True,
                trade_date=date(2026, 9, 7), ttl_map=None):
    get_shared_stats().reset()
    return ResponseCachePolicy(
        db_path=str(tmp_path / "cache.db"),
        ttl_map=dict(ttl_map or TTL_MAP),
        category_map=dict(CATEGORY_MAP),
        enabled=enabled,
        trading_hours_fn=lambda: not phase_offhours,
        effective_date_fn=lambda: trade_date,
    )


class FakeMsg:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """计数 fake 模型：验证「命中短路不产生 LLM 调用」（AC-1）。"""

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


def make_gateway(monkeypatch, cache_policy, routing_policy):
    """构造带 fake 后端 + 显式 policy 的网关（不依赖真实 API）。"""
    created = []

    def _build(profile):
        m = FakeModel(profile.model)
        created.append(m)
        return m

    monkeypatch.setattr(gateway_mod, "build_chat_model", _build)
    gw = LLMGateway(registry=ModelRegistry(yaml_path=None))
    gw._cache_policy_inst = cache_policy
    gw._routing_policy_inst = routing_policy
    gw._created_models = created
    return gw


# ── 键归一化（AC-8 / FR-02） ──

def test_normalize_query_strips_whitespace():
    assert normalize_query("  今天大盘怎么看  ") == "今天大盘怎么看"


def test_normalize_query_fullwidth_to_halfwidth():
    assert normalize_query("ＡＢＣ　ｄｅ") == "ABC de"
    assert normalize_query("今天大盘怎么看？") == "今天大盘怎么看"


def test_normalize_query_tail_particles():
    assert normalize_query("今天大盘怎么样呢") == "今天大盘怎么样"
    assert normalize_query("今天大盘怎么看？呢") == "今天大盘怎么看"
    assert normalize_query("看看牧原吧。") == "看看牧原"


def test_normalize_query_collapses_whitespace():
    assert normalize_query("大盘   怎么\t看") == "大盘 怎么 看"


def test_normalize_synonym_queries_same_key(tmp_path_factory):
    """同义同形（归一化后相同）→ 同键命中（FR-02）。"""
    p = make_policy(tmp_path_factory.mktemp("norm"))
    q1 = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看？ ",
                     metadata=GOOD_META, phase="offhours")
    q2 = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                     metadata=GOOD_META, phase="offhours")
    assert q1 == q2


# ── 键组成与隔离 ──

def test_build_key_shape(tmp_path_factory):
    p = make_policy(tmp_path_factory.mktemp("shape"))
    key = p.build_key(purpose="agent", label="report", query="今天大盘怎么看",
                      metadata=GOOD_META, phase="offhours")
    assert key.startswith(KEY_PREFIX + "v1:agent:report:market_quick:tpl1:offhours:abc123:")


def test_different_inject_hash_different_key(tmp_path_factory):
    """注入哈希变化 → key 变化（AC-8，有/无持仓注入互不命中 AC-3）。"""
    p = make_policy(tmp_path_factory.mktemp("inj"))
    k1 = p.build_key(purpose="agent", label="agent", query="q",
                     metadata=dict(GOOD_META, sr_inject_hash="hold-a"), phase="offhours")
    k2 = p.build_key(purpose="agent", label="agent", query="q",
                     metadata=dict(GOOD_META, sr_inject_hash="hold-b"), phase="offhours")
    k3 = p.build_key(purpose="agent", label="agent", query="q",
                     metadata=dict(GOOD_META, sr_inject_hash="hold-a"), phase="offhours")
    assert k1 != k2
    assert k1 == k3


def test_missing_inject_hash_must_miss(tmp_path_factory):
    """sr_inject_hash 缺失视为有注入 → 键取随机值，两次构造必不同（保守原则）。"""
    p = make_policy(tmp_path_factory.mktemp("noinj"))
    meta = dict(GOOD_META)
    meta.pop("sr_inject_hash")
    k1 = p.build_key(purpose="agent", label="agent", query="q", metadata=meta, phase="offhours")
    k2 = p.build_key(purpose="agent", label="agent", query="q", metadata=meta, phase="offhours")
    assert k1 is not None and k2 is not None
    assert k1 != k2


def test_different_market_phase_different_key(tmp_path_factory):
    """盘中/盘后互不命中（AC-2）：phase 入键。"""
    p = make_policy(tmp_path_factory.mktemp("phase"),
                    ttl_map=dict(TTL_MAP, sector_intraday=60))
    meta = dict(GOOD_META, sr_scenario="sector_analysis")
    k_intraday = p.build_key(purpose="agent", label="agent", query="电池板块为啥涨",
                             metadata=meta, phase="intraday")
    k_off = p.build_key(purpose="agent", label="agent", query="电池板块为啥涨",
                        metadata=meta, phase="offhours")
    assert k_intraday != k_off


# ── 保守原则：metadata 缺失不缓存 ──

def test_metadata_missing_not_cacheable(tmp_path_factory):
    """无 sr_* 键 / 无 sr_cacheable / sr_cacheable=False → 一律不缓存（宁 miss 不错缓存）。"""
    p = make_policy(tmp_path_factory.mktemp("conservative"))
    assert p.build_key(purpose="agent", label="agent", query="q", metadata={}, phase="offhours") is None
    assert p.build_key(purpose="agent", label="agent", query="q", metadata=None, phase="offhours") is None
    no_flag = {k: v for k, v in GOOD_META.items() if k != "sr_cacheable"}
    assert p.build_key(purpose="agent", label="agent", query="q", metadata=no_flag, phase="offhours") is None
    assert p.build_key(purpose="agent", label="agent", query="q",
                       metadata=dict(GOOD_META, sr_cacheable=False), phase="offhours") is None


# ── TTL 分级（FR-03） ──

def test_sector_intraday_no_cache(tmp_path_factory):
    """盘中板块/个股分析禁缓存（穿透率 100%，FR-03）。"""
    p = make_policy(tmp_path_factory.mktemp("intraday"))
    meta = dict(GOOD_META, sr_scenario="sector_analysis")
    assert p.resolve_ttl("sector", "intraday") == 0
    assert p.build_key(purpose="agent", label="agent", query="电池板块为啥涨",
                       metadata=meta, phase="intraday") is None


def test_data_query_no_cache(tmp_path_factory):
    p = make_policy(tmp_path_factory.mktemp("dq"))
    meta = dict(GOOD_META, sr_scenario="data_query")
    assert p.build_key(purpose="agent", label="agent", query="600519 现价",
                       metadata=meta, phase="offhours") is None


def test_market_phase_offhours_cacheable(tmp_path_factory):
    p = make_policy(tmp_path_factory.mktemp("ok"))
    key = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                      metadata=GOOD_META, phase="offhours")
    assert key is not None


# ── 读写与 TTL 过期（FR-04） ──

def test_store_and_lookup_roundtrip(tmp_path):
    p = make_policy(tmp_path)
    key = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                      metadata=GOOD_META, phase="offhours")
    assert p.store(key, "market", "看多", model="m1", ttl_s=1800, tier="fast",
                   query_head="今天大盘怎么看")
    entry = p.lookup(key, "market")
    assert isinstance(entry, CacheEntry)
    assert entry.response == "看多"
    assert entry.model == "m1"
    assert entry.tier == "fast"
    assert entry.created_trade_date == "2026-09-07"


def test_ttl_expiry_means_miss(tmp_path):
    p = make_policy(tmp_path)
    key = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                      metadata=GOOD_META, phase="offhours")
    p.store(key, "market", "看多", model="m1", ttl_s=1800)
    # 直改库内 expire_at 模拟过期
    with sqlite3.connect(str(tmp_path / "cache.db")) as conn:
        conn.execute("UPDATE cache_kv SET expire_at='2000-01-01 00:00:00' WHERE cache_key=?",
                     (key,))
        conn.commit()
    assert p.lookup(key, "market") is None
    # 过期条目应被清除
    with sqlite3.connect(str(tmp_path / "cache.db")) as conn:
        n = conn.execute("SELECT COUNT(*) FROM cache_kv WHERE cache_key=?", (key,)).fetchone()[0]
    assert n == 0


def test_cross_day_invalidation_guarded_categories(tmp_path):
    """市场观点类跨交易日失效（AC-4）；方法论类不启用跨日守卫（Q1 裁决）。"""
    p1 = make_policy(tmp_path, trade_date=date(2026, 9, 7))
    k_market = p1.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                            metadata=GOOD_META, phase="offhours")
    p1.store(k_market, "market", "看多", model="m1", ttl_s=1800)
    k_meth = p1.build_key(purpose="agent", label="agent", query="如何做仓位管理",
                          metadata=dict(GOOD_META, sr_scenario="methodology"), phase="offhours")
    p1.store(k_meth, "methodology", "先定风险", model="m1", ttl_s=604800)

    # 次日：市场观点类失效；方法论类仍命中
    p2 = make_policy(tmp_path, trade_date=date(2026, 9, 8))
    assert p2.lookup(k_market, "market") is None
    entry = p2.lookup(k_meth, "methodology")
    assert entry is not None and entry.response == "先定风险"


def test_large_response_not_stored(tmp_path):
    """响应 >200KB 不存（设计 §7.3）。"""
    p = make_policy(tmp_path)
    key = p.build_key(purpose="agent", label="agent", query="q",
                      metadata=GOOD_META, phase="offhours")
    big = "x" * (201 * 1024)
    assert p.store(key, "market", big, model="m1", ttl_s=1800) is False
    assert p.lookup(key, "market") is None


# ── 开关关闭回退（AC-6，policy 级） ──

def test_disabled_policy_is_noop(tmp_path):
    p = make_policy(tmp_path, enabled=False)
    assert p.build_key(purpose="agent", label="agent", query="q",
                       metadata=GOOD_META, phase="offhours") is None
    assert p.store(KEY_PREFIX + "manual", "market", "x", model="m", ttl_s=60) is False
    assert p.lookup(KEY_PREFIX + "manual", "market") is None


# ── 计量与运维 ──

def test_stats_counters(tmp_path):
    p = make_policy(tmp_path)
    key = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                      metadata=GOOD_META, phase="offhours")
    p.store(key, "market", "看多", model="m1", ttl_s=1800)
    p.lookup(key, "market")                       # 命中
    p.record_hit("market", saved_tokens=120)
    p.lookup(KEY_PREFIX + "v1:other", "market")   # 未命中
    s = p.stats()
    assert s["enabled"] is True
    assert s["entries"] == 1
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert s["hit_rate"] == 0.5
    assert s["saved_tokens_est"] == 120
    assert s["by_category"]["market"] == {"hits": 1, "misses": 1}


def test_clear_with_prefix(tmp_path):
    p = make_policy(tmp_path)
    key = p.build_key(purpose="agent", label="agent", query="q",
                      metadata=GOOD_META, phase="offhours")
    p.store(key, "market", "x", model="m", ttl_s=60)
    assert p.clear() >= 1
    assert p.stats()["entries"] == 0


# ── Gateway 集成：命中短路不产生 LLM 调用（AC-1，mock 计数） ──

def test_gateway_cache_hit_skips_llm(tmp_path, monkeypatch):
    p = make_policy(tmp_path)
    gw = make_gateway(monkeypatch, p, ModelRoutingPolicy(enabled=False))
    kw = dict(logger=logging.getLogger("t"), label="agent",
              metadata=dict(GOOD_META))
    r1 = gw.invoke("agent", [("user", "今天大盘怎么看")], **kw)
    r2 = gw.invoke("agent", [("user", "今天大盘怎么看")], **kw)
    # 第 2 次命中缓存：仍只有 1 次真实 LLM 调用，响应一致
    assert sum(m.calls for m in gw._created_models) == 1
    assert r2.content == r1.content
    assert get_shared_stats().snapshot()["hits"] == 1


def test_gateway_cache_disabled_baseline(tmp_path, monkeypatch):
    """关闭缓存（AC-6）：第 2 次请求恢复全价调用，且 metadata 不被注入计量字段。"""
    p = make_policy(tmp_path, enabled=False)
    gw = make_gateway(monkeypatch, p, ModelRoutingPolicy(enabled=False))
    kw = dict(logger=logging.getLogger("t"), label="agent",
              metadata=dict(GOOD_META))
    gw.invoke("agent", [("user", "今天大盘怎么看")], **kw)
    gw.invoke("agent", [("user", "今天大盘怎么看")], **kw)
    assert sum(m.calls for m in gw._created_models) == 2
    md = (gw._created_models[0].last_config or {}).get("metadata") or {}
    assert "cache_hit" not in md and "model_tier" not in md


def test_gateway_not_cacheable_bypasses_cache(tmp_path, monkeypatch):
    """无 sr_cacheable 的调用（个性化路径）永远全价：两次调用两次 LLM。"""
    p = make_policy(tmp_path)
    gw = make_gateway(monkeypatch, p, ModelRoutingPolicy(enabled=False))
    kw = dict(logger=logging.getLogger("t"), label="agent",
              metadata={"sr_scenario": "market_quick"})  # 缺 sr_cacheable → 不缓存
    gw.invoke("agent", [("user", "今天大盘怎么看")], **kw)
    gw.invoke("agent", [("user", "今天大盘怎么看")], **kw)
    assert sum(m.calls for m in gw._created_models) == 2


def test_gateway_stream_never_uses_cache(tmp_path, monkeypatch):
    """流式链一期不接缓存：即便缓存已有同键条目，流式仍走真实 LLM。"""
    p = make_policy(tmp_path)
    key = p.build_key(purpose="agent", label="agent", query="今天大盘怎么看",
                      metadata=GOOD_META, phase="offhours")
    p.store(key, "market", "缓存答案", model="m", ttl_s=1800)
    gw = make_gateway(monkeypatch, p, ModelRoutingPolicy(enabled=False))
    llm = gw.chat("agent")
    chunks = list(llm.stream([("user", "今天大盘怎么看")],
                             {"metadata": dict(GOOD_META)}))
    assert sum(m.calls for m in gw._created_models) == 1
    assert all("缓存答案" != c.content for c in chunks)

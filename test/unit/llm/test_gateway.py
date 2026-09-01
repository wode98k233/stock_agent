"""LLMGateway 端到端测试：策略链全链路 + embed/rerank 路由（fake backend，不依赖真实 API）。"""
import asyncio
import json
import logging

import pytest

# 注意：gateway_mod 必须是「模块」utils.llm.gateway（含模块级 build_chat_model /
# EmbeddingProvider / RerankProvider），不是 utils.llm 包里的 gateway 单例实例。
# 由于 utils/llm/__init__.py 导出了同名 gateway 单例属性，会遮蔽子模块，
# `import utils.llm.gateway as x` 经属性访问会取到实例；故用 importlib 直接取模块对象。
import importlib

gateway_mod = importlib.import_module("utils.llm.gateway")
from utils.llm.gateway import LLMGateway
from utils.llm.registry import ModelRegistry
from utils.llm.policies.ratelimit import RateLimited


class FakeMsg:
    def __init__(self, content):
        self.content = content


class FakeChunk:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """model_name 为 'bad'/'bad2' 时按 fail_with 抛错，否则返回成功消息 / chunk。"""

    def __init__(self, model, fail_with=None):
        self.model_name = model
        self._fail = fail_with
        self.calls = 0
        self.last_config = None

    def invoke(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        if self._fail:
            raise self._fail
        return FakeMsg("ok-from-" + self.model_name)

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        if self._fail:
            raise self._fail
        return FakeMsg("ok-from-" + self.model_name)

    def stream(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        if self._fail:
            raise self._fail

        def gen():
            yield FakeChunk("c1-" + self.model_name)
            yield FakeChunk("c2-" + self.model_name)

        return gen()

    async def astream(self, messages, config=None):
        self.calls += 1
        self.last_config = config
        if self._fail:
            raise self._fail
        yield FakeChunk("c1-" + self.model_name)
        yield FakeChunk("c2-" + self.model_name)


def _fake_build(profile):
    if profile.model == "bad":
        return FakeModel(profile.model, fail_with=ConnectionError("boom"))
    if profile.model == "bad2":
        return FakeModel(profile.model, fail_with=ValueError("fatal"))
    return FakeModel(profile.model)


def _make_gateway(chat_override, breaker_threshold=1):
    reg = ModelRegistry(yaml_path=None)
    # 用 monkeypatch 不方便在 fixture 外，改为直接构造带 yaml 的 registry
    p = "/tmp/_noop"  # 不会用到
    reg = ModelRegistry(yaml_path=None)
    # 通过临时文件覆盖
    import tempfile, os
    d = tempfile.mkdtemp()
    f = os.path.join(d, "m.json")
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(chat_override, fh)
    reg = ModelRegistry(yaml_path=f)
    return LLMGateway(registry=reg, breaker_threshold=breaker_threshold)


# ── 同步：主模型失败 → 熔断 → 降级到 fallback ──

def test_sync_fallback_on_failure(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    monkeypatch.setattr("utils.llm_factory._retry_delay", lambda attempt: 0)  # 控制时序：退避置 0
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "bad"}, "fallbacks": ["good"]}}})
    res = gw.invoke("agent", [("user", "hi")], logging.getLogger("t"), label="agent")
    assert res.content == "ok-from-good"


# ── 同步：非重试错误直接抛，不降级 ──

def test_non_retryable_raises(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "bad2"}, "fallbacks": ["good"]}}})
    with pytest.raises(ValueError):
        gw.invoke("agent", [("user", "hi")], logging.getLogger("t"), label="agent")


# ── 异步：主模型失败 → 降级 ──

def test_async_fallback_on_failure(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    monkeypatch.setattr("utils.llm_factory._retry_delay", lambda attempt: 0)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "bad"}, "fallbacks": ["good"]}}})

    async def run():
        return await gw.ainvoke("agent", [("user", "hi")], logging.getLogger("t"), label="agent")

    res = asyncio.run(run())
    assert res.content == "ok-from-good"


# ── 限流：tpm 过小 → 抛 RateLimited（不降级） ──

def test_ratelimit_raises(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    gw = _make_gateway({
        "chat": {"agent": {"primary": {"model": "good"}}},
        "limits": {"agent": {"rpm": 0, "tpm": 1}},  # 一条长消息就超限
    })
    with pytest.raises(RateLimited):
        gw.invoke("agent", [("user", "x" * 100)], logging.getLogger("t"), label="agent")


# ── embed 路由（一等公民） ──

def test_embed_routing(monkeypatch):
    class FakeEmbed:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(gateway_mod, "EmbeddingProvider", FakeEmbed)
    gw = LLMGateway(registry=ModelRegistry(yaml_path=None))
    assert gw.embed(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]


# ── rerank 路由（一等公民） ──

def test_rerank_routing(monkeypatch):
    class FakeRerank:
        def __init__(self, spec):
            self.spec = spec

        def rerank(self, query, docs):
            return [0.5, 0.1]

        def warm(self):
            pass

    monkeypatch.setattr(gateway_mod, "RerankProvider", FakeRerank)
    gw = LLMGateway(registry=ModelRegistry(yaml_path=None))
    assert gw.rerank("q", ["d1", "d2"]) == [0.5, 0.1]


# ── 缺口1修复：链内按位置传的 config 必须透传到底层（否则 LangGraph 回调/run_id 断裂） ──

def test_invoke_passes_positional_config(monkeypatch):
    captured = {}
    base = _fake_build

    def _build(profile):
        m = base(profile)
        captured.setdefault("model", m)
        return m

    monkeypatch.setattr(gateway_mod, "build_chat_model", _build)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "good"}}}})
    llm = gw.chat("agent")
    cfg = {"callbacks": ["SOME_CALLBACK"], "tags": ["x"]}
    llm.invoke([("user", "hi")], cfg)  # 链内部按位置传 config
    # 透传到底层：tags 保留（证明位置 config 没被吞）
    assert captured["model"].last_config is not None
    assert captured["model"].last_config.get("tags") == ["x"]
    # 原 callbacks 也保留（tracked_invoke 注入 TokenRecorder 后不会丢弃链上回调）
    assert "SOME_CALLBACK" in captured["model"].last_config["callbacks"]


# ── 缺口2修复：流式走网关（限流/熔断/降级）；首 chunk 前失败才降级 ──

def test_stream_runs_through_gateway(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "good"}}}})
    llm = gw.chat("agent")
    chunks = list(llm.stream([("user", "hi")]))
    assert [c.content for c in chunks] == ["c1-good", "c2-good"]


def test_stream_fallback_on_pre_first_chunk_failure(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    monkeypatch.setattr("utils.llm_factory._retry_delay", lambda attempt: 0)
    # primary "bad" 的 stream 立即抛 ConnectionError（首 chunk 前）→ 降级到 good
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "bad"}, "fallbacks": ["good"]}}}, breaker_threshold=1)
    llm = gw.chat("agent")
    chunks = list(llm.stream([("user", "hi")]))
    assert [c.content for c in chunks] == ["c1-good", "c2-good"]


def test_stream_passes_config(monkeypatch):
    captured = {}
    base = _fake_build

    def _build(profile):
        m = base(profile)
        captured.setdefault("model", m)
        return m

    monkeypatch.setattr(gateway_mod, "build_chat_model", _build)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "good"}}}})
    llm = gw.chat("agent")
    cfg = {"tags": ["stream-tag"]}
    list(llm.stream([("user", "hi")], cfg))
    assert captured["model"].last_config is not None
    assert captured["model"].last_config.get("tags") == ["stream-tag"]


def test_astream_runs_through_gateway(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "good"}}}})
    llm = gw.chat("agent")

    async def run():
        return [c async for c in llm.astream([("user", "hi")])]

    chunks = asyncio.run(run())
    assert [c.content for c in chunks] == ["c1-good", "c2-good"]


def test_gateway_stream_convenience(monkeypatch):
    monkeypatch.setattr(gateway_mod, "build_chat_model", _fake_build)
    gw = _make_gateway({"chat": {"agent": {"primary": {"model": "good"}}}})
    chunks = list(gw.stream("agent", [("user", "hi")]))
    assert [c.content for c in chunks] == ["c1-good", "c2-good"]

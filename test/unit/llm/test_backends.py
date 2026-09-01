"""backend 层测试：chat 构造、embed/rerank 分发逻辑（用 mock，不依赖真实 API）。"""
import pytest

from utils.llm.backends.chat import build_chat_model
from utils.llm.backends.embed import EmbeddingProvider
from utils.llm.backends.rerank import RerankProvider
from utils.llm.types import ModelProfile, EmbedSpec, RerankSpec, Purpose


# ── chat ──

def test_build_chat_model_sets_profile():
    p = ModelProfile(model="test-model", api_key="k", base_url="http://x/v1", temperature=0.2)
    llm = build_chat_model(p)
    assert llm.model_name == "test-model"
    assert llm.openai_api_base == "http://x/v1"


# ── embed 分发 ──

def test_embed_openai_dispatch(monkeypatch):
    spec = EmbedSpec(provider="openai", model="emb", api_key="k", base_url="http://x/v1")
    prov = EmbeddingProvider(spec)
    monkeypatch.setattr(prov, "_embed_openai", lambda texts: [[0.1, 0.2] for _ in texts])
    assert prov.embed(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]


def test_embed_ollama_dispatch(monkeypatch):
    spec = EmbedSpec(provider="ollama", model="emb")
    prov = EmbeddingProvider(spec)
    monkeypatch.setattr(prov, "_embed_ollama", lambda texts: [[0.3] for _ in texts])
    assert prov.embed(["a"]) == [[0.3]]


def test_embed_local_dispatch(monkeypatch):
    spec = EmbedSpec(provider="local", local_model="BAAI/bge-small-zh-v1.5")
    prov = EmbeddingProvider(spec)
    monkeypatch.setattr(prov, "_embed_local", lambda texts: [[0.9] for _ in texts])
    assert prov.embed(["a"]) == [[0.9]]


# ── rerank 分发与可用性 ──

def test_rerank_local_available_and_dispatch(monkeypatch):
    spec = RerankSpec(provider="local", model="BAAI/bge-reranker-v2-m3", local_dir="/m")
    prov = RerankProvider(spec)
    assert prov.is_available() is True
    monkeypatch.setattr(prov, "_ensure_local_model", lambda: True)
    monkeypatch.setattr(prov, "_model", object())  # 标记已加载

    class _FakeArr:
        def tolist(self):
            return [0.5, 0.1]

    monkeypatch.setattr(prov, "_model", type("M", (), {"predict": staticmethod(lambda pairs, **kw: _FakeArr())})())
    scores = prov.rerank("q", ["d1", "d2"])
    assert scores == [0.5, 0.1]


def test_rerank_openai_unavailable_without_creds():
    spec = RerankSpec(provider="openai", model="m")
    prov = RerankProvider(spec)  # 无 api_key/base_url → 不可用
    assert prov.is_available() is False
    assert prov.rerank("q", ["d1"]) == [0.0]  # 静默降级


def test_rerank_openai_http_call(monkeypatch):
    """openai 模式走原生 HTTP POST {base}/rerank（OpenAI SDK 无 client.rerank，必须直接调）"""
    spec = RerankSpec(provider="openai", model="BAAI/bge-reranker-v2-m3",
                      api_key="k", base_url="https://api.example.com/v1/")
    prov = RerankProvider(spec)
    assert prov.is_available() is True

    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.85},
                                {"index": 0, "relevance_score": 0.42},
                                {"index": 2, "relevance_score": 0.05}]}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr("httpx.post", _fake_post)
    scores = prov.rerank("q", ["d1", "d2", "d3"])
    # 按 index 还原为原 docs 顺序
    assert scores == [0.42, 0.85, 0.05]
    # 端点拼接正确：base_url 去尾斜杠 + /v1/rerank
    assert captured["url"] == "https://api.example.com/v1/rerank"
    assert captured["json"]["model"] == "BAAI/bge-reranker-v2-m3"
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_rerank_openai_http_base_url_without_v1(monkeypatch):
    """base_url 不带 /v1 时自动补全"""
    spec = RerankSpec(provider="openai", model="m", api_key="k",
                      base_url="https://api.example.com")
    prov = RerankProvider(spec)
    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResp()

    monkeypatch.setattr("httpx.post", _fake_post)
    prov.rerank("q", ["d1"])
    assert captured["url"] == "https://api.example.com/v1/rerank"


def test_rerank_degrades_on_local_load_failure(monkeypatch):
    spec = RerankSpec(provider="local", model="bad-model")
    prov = RerankProvider(spec)
    monkeypatch.setattr(prov, "_ensure_local_model", lambda: False)
    assert prov.rerank("q", ["d1", "d2"]) == [0.0, 0.0]

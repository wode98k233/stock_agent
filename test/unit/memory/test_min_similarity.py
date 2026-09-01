"""语义相似度门槛 (min_similarity) 相关单测。"""
import pytest

from memory.backend.base import MemoryEntry
from memory.backend.embedding import EmbeddingBackend, _filter_by_min_similarity
from memory.sdk import (
    MemorySDK,
    MemoryConfig,
    MIN_SIMILARITY_DEFAULT,
    load_memory_config,
    save_memory_config,
)


def _mk(score, eid="e1"):
    return MemoryEntry(entry_id=eid, stock_code="", stock_name="", content="", score=score)


# ── 过滤辅助函数 ─────────────────────────────────────────

def test_filter_no_threshold_returns_all():
    entries = [_mk(0.1), _mk(0.9), _mk(0.0)]
    assert _filter_by_min_similarity(entries, 0.0) == entries
    assert _filter_by_min_similarity(entries, 0) == entries
    assert _filter_by_min_similarity(entries, None) == entries


def test_filter_drops_below_threshold_keeps_order():
    entries = [_mk(0.75, "a"), _mk(0.5, "b"), _mk(0.8, "c"), _mk(0.6, "d")]
    out = _filter_by_min_similarity(entries, 0.72)
    assert [e.entry_id for e in out] == ["a", "c"]


def test_filter_threshold_exactly_equal_kept():
    entries = [_mk(0.72), _mk(0.71)]
    out = _filter_by_min_similarity(entries, 0.72)
    assert len(out) == 1 and out[0].score == 0.72


# ── EmbeddingBackend.search 应用门槛 ─────────────────────

def test_embedding_backend_search_applies_threshold(tmp_path):
    emb = EmbeddingBackend(embedding_fn=lambda x: [0.0] * 3, chroma_path=str(tmp_path / "chroma"))
    emb._available = True
    # 伪造 ChromaDB 返回：按距离升序（相似度降序）
    emb._collection.query = lambda **kw: {
        "ids": [["e1", "e2", "e3"]],
        "metadatas": [[{"stock_name": "大盘", "stock_code": ""},
                       {"stock_name": "医药", "stock_code": ""},
                       {"stock_name": "游戏", "stock_code": ""}]],
        "documents": [["a", "b", "c"]],
        "distances": [[0.2, 0.8, 0.9]],  # 相似度 0.9 / 0.6 / 0.55
    }
    emb._min_similarity = 0.72
    out = emb.search("大盘", top_k=10)
    scores = [e.score for e in out]
    assert all(s >= 0.72 for s in scores)
    assert scores == [0.9]  # 仅 e1 通过


def test_embedding_backend_search_threshold_disabled_returns_all(tmp_path):
    emb = EmbeddingBackend(embedding_fn=lambda x: [0.0] * 3, chroma_path=str(tmp_path / "chroma"))
    emb._available = True
    emb._collection.query = lambda **kw: {
        "ids": [["e1", "e2"]],
        "metadatas": [[{"stock_name": "x", "stock_code": ""}, {"stock_name": "y", "stock_code": ""}]],
        "documents": [["a", "b"]],
        "distances": [[0.9, 0.9]],  # 相似度 0.55
    }
    emb._min_similarity = 0.0
    out = emb.search("任意", top_k=10)
    assert len(out) == 2


# ── SDK 配置默认值与持久化 ──────────────────────────────

def test_min_similarity_default():
    assert MIN_SIMILARITY_DEFAULT == 0.72
    assert MemoryConfig().min_similarity == 0.72


def test_memory_config_persist_roundtrip(tmp_path, monkeypatch):
    import memory.sdk as sdk_mod
    path = tmp_path / "memory_config.json"
    monkeypatch.setattr(sdk_mod, "get_memory_config_path", lambda: str(path))

    # 默认
    assert load_memory_config()["min_similarity"] == 0.72

    save_memory_config({"min_similarity": 0.8})
    assert load_memory_config()["min_similarity"] == 0.8


def test_sdk_set_min_similarity_updates_and_persists(tmp_path, monkeypatch):
    import memory.sdk as sdk_mod
    path = tmp_path / "memory_config.json"
    monkeypatch.setattr(sdk_mod, "get_memory_config_path", lambda: str(path))

    class _FakeEmb:
        def __init__(self):
            self.v = None

        def set_min_similarity(self, x):
            self.v = x

    class _FakeBackend:
        def __init__(self):
            self._embedding = _FakeEmb()

    sdk = MemorySDK(MemoryConfig())
    sdk._backend = _FakeBackend()
    sdk._initialized = True

    res = sdk.set_min_similarity(0.8)
    assert res == 0.8
    assert sdk._backend._embedding.v == 0.8
    assert sdk.get_min_similarity() == 0.8
    # 持久化
    import json
    assert json.loads(path.read_text(encoding="utf-8"))["min_similarity"] == 0.8

    with pytest.raises(ValueError):
        sdk.set_min_similarity(1.5)
    with pytest.raises(ValueError):
        sdk.set_min_similarity(-0.1)

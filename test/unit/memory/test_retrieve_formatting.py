"""retrieve 注入格式化：按组取多块 + 文档序 + 字符预算（2026-09-23 P0-3）。"""
from memory.backend.base import MemoryEntry
from memory.sdk import MemorySDK, MemoryConfig


class _FakeBackend:
    def __init__(self, entries):
        self._entries = entries

    def name(self):
        return "fake"

    def search(self, query, top_k=10, confidence_threshold=0.0, context=None):
        return list(self._entries[:top_k])


def _entry(i, gid="g1", content=None):
    return MemoryEntry(
        entry_id=f"e{i}", stock_code="002714", stock_name="牧原股份",
        content=content if content is not None else f"chunk{i}-" + "x" * 120,
        score=0.9,
        metadata={"date": "2026-09-23T19:00:00", "source_query": "q",
                  "tags": [], "chunk_index": i, "chunk_count": 10, "chunk_group_id": gid},
        confidence=0.9,
    )


def _sdk(entries, **cfg):
    sdk = MemorySDK(MemoryConfig(
        backend_mode="fts5", min_similarity=0.0, similarity_margin=0.0,
        confidence_threshold=0.0, retrieval_top_k=20, **cfg))
    sdk._backend = _FakeBackend(entries)
    sdk._initialized = True
    return sdk


def test_retrieve_injects_more_than_three_chunks():
    text = _sdk([_entry(i) for i in range(10)],
                max_chunks_per_group=6, chunk_max_chars=1000, max_prompt_chars=8000).retrieve("q")
    assert "chunk0-" in text and "chunk5-" in text
    assert "chunk6-" not in text  # 超过 per_group 上限


def test_retrieve_document_order_within_group():
    # 输入顺序打乱（2,0,1），注入应按 chunk_index 升序
    text = _sdk([_entry(2), _entry(0), _entry(1)],
                max_chunks_per_group=6, max_prompt_chars=8000).retrieve("q")
    assert text.index("chunk0-") < text.index("chunk1-") < text.index("chunk2-")


def test_retrieve_respects_char_budget():
    sdk = _sdk([_entry(i) for i in range(10)],
               max_chunks_per_group=10, chunk_max_chars=1000, max_prompt_chars=300)
    text = sdk.retrieve("q")
    assert len(text) <= 300


def test_retrieve_chunk_char_limit():
    e = _entry(0, content="A" * 2000)
    text = _sdk([e], max_chunks_per_group=6, chunk_max_chars=100, max_prompt_chars=8000).retrieve("q")
    # 单块被截到 ~100 字符（不含标题行）
    assert "A" * 500 not in text

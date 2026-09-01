"""Unit tests for EmbeddingBackend and HybridBackend integration."""
import sys
import os
# Ensure project root in sys.path (needed for pytest >= 9)
_srcdir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _srcdir not in sys.path:
    sys.path.insert(0, _srcdir)

import json
import pytest
from unittest.mock import MagicMock, patch, ANY

from memory.backend import (
    EmbeddingBackend, HybridBackend, FTS5Backend,
    MemoryEntry, create_memory_backend,
)


# ============================================================
# EmbeddingBackend — 构造器
# ============================================================

class TestEmbeddingBackendInit:
    """Constructor behavior."""

    def test_unavailable_when_embedding_fn_is_none(self):
        eb = EmbeddingBackend(embedding_fn=None)
        assert eb.is_available() is False
        assert eb.name() == "embedding"

    def test_unavailable_when_chromadb_import_fails(self):
        # Patch builtins.__import__ to simulate chromadb not being installed
        import builtins
        _orig_import = builtins.__import__
        def _fake_import(name, *args, **kwargs):
            if name == "chromadb":
                raise ImportError("No module named 'chromadb'")
            return _orig_import(name, *args, **kwargs)
        builtins.__import__ = _fake_import
        try:
            eb = EmbeddingBackend(embedding_fn=lambda t: [0.1] * 16)
            assert eb.is_available() is False
        finally:
            builtins.__import__ = _orig_import

    def test_available_when_both_present(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        assert eb.is_available() is True
        assert eb.name() == "embedding"

    def test_creates_persistent_client_with_correct_path(self, mock_chromadb, mock_embedding_fn):
        with patch("memory.backend.embedding.get_chroma_data_path", return_value="/fake/chroma"):
            EmbeddingBackend(embedding_fn=mock_embedding_fn)
            mock_chromadb["client_class"].assert_called_once_with(path="/fake/chroma")

    def test_creates_or_gets_collection_with_cosine_space(self, mock_chromadb, mock_embedding_fn):
        EmbeddingBackend(embedding_fn=mock_embedding_fn)
        mock_chromadb["client"].get_or_create_collection.assert_called_once_with(
            name="stock_memory",
            metadata={"hnsw:space": "cosine"},
        )

    def test_accepts_custom_chroma_path(self, mock_chromadb, mock_embedding_fn):
        EmbeddingBackend(embedding_fn=mock_embedding_fn, chroma_path="/custom/path")
        mock_chromadb["client_class"].assert_called_once_with(path="/custom/path")


# ============================================================
# EmbeddingBackend — 元数据序列化
# ============================================================

class TestMetadataSerialization:
    """Round-trip integrity for tag serialization."""

    def test_tags_roundtrip(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entry = MemoryEntry(
            entry_id="test", stock_code="000001", stock_name="test",
            content="test", metadata={"tags": ["低估值", "银行股"], "pe": 5.2}
        )
        chroma_meta = eb._build_chroma_metadata(entry)
        assert isinstance(chroma_meta["tags"], str)
        parsed, conf, trace_id = eb._parse_chroma_metadata(chroma_meta)
        assert parsed["tags"] == ["低估值", "银行股"]
        assert conf == 0.5  # v2: default confidence

    def test_empty_tags_roundtrip(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entry = MemoryEntry(
            entry_id="test", stock_code="000001", stock_name="test",
            content="test", metadata={"tags": []}
        )
        chroma_meta = eb._build_chroma_metadata(entry)
        parsed, conf, trace_id = eb._parse_chroma_metadata(chroma_meta)
        assert parsed["tags"] == []

    def test_no_metadata_tags_roundtrip(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entry = MemoryEntry(
            entry_id="test", stock_code="000001", stock_name="test",
            content="test", metadata={}
        )
        chroma_meta = eb._build_chroma_metadata(entry)
        parsed, conf, trace_id = eb._parse_chroma_metadata(chroma_meta)
        assert parsed["tags"] == []

    def test_none_fields_defaulted(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        chroma_meta = eb._build_chroma_metadata(MemoryEntry(
            entry_id="test", stock_code="", stock_name="", content="test"
        ))
        assert chroma_meta["sector_l1"] == ""
        assert chroma_meta["sector_l2"] == ""
        assert chroma_meta["pe"] == 0.0
        assert chroma_meta["roe"] == 0.0
        assert chroma_meta["volatility"] == ""
        assert chroma_meta["sentiment"] == ""

    def test_parse_invalid_tags_json(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        parsed, conf, trace_id = eb._parse_chroma_metadata({"tags": "not valid json[[["})
        assert parsed["tags"] == []

    def test_parse_none_tags(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        parsed, conf, trace_id = eb._parse_chroma_metadata({"tags": None})
        assert parsed["tags"] == []

    def test_lifecycle_metadata_roundtrip(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entry = MemoryEntry(
            entry_id="life-1", stock_code="000001", stock_name="平安银行",
            content="生命周期测试",
            metadata={"subject_kind": "stock", "date": "2026-07-01T00:00:00"},
            confidence=0.88,
            confidence_factors={"base": 0.8},
            provenance={"source": "provider-a", "trace_run_id": "trace-1"},
            memory_category="valuation",
            ttl_days=90,
            expires_at="2026-10-01T00:00:00",
            last_validated_at="2026-07-02T00:00:00",
            superseded_by="new-1",
            superseded_at="2026-07-03T00:00:00",
            supersede_reason="new_analysis",
        )
        chroma_meta = eb._build_chroma_metadata(entry)
        mock_chromadb["collection"].query.return_value = {
            "ids": [["life-1"]],
            "metadatas": [[chroma_meta]],
            "documents": [[entry.content]],
            "distances": [[0.1]],
        }

        result = eb.search("生命周期", top_k=1, include_inactive=True)[0]

        assert result.metadata["subject_kind"] == "stock"
        assert result.confidence_factors == {"base": 0.8}
        assert result.provenance["source"] == "provider-a"
        assert result.memory_category == "valuation"
        assert result.ttl_days == 90
        assert result.last_validated_at == "2026-07-02T00:00:00"
        assert result.superseded_by == "new-1"
        assert result.supersede_reason == "new_analysis"


# ============================================================
# EmbeddingBackend — 写入
# ============================================================

class TestEmbeddingBackendAdd:
    """Write behavior."""

    def test_add_raises_when_unavailable(self):
        eb = EmbeddingBackend(embedding_fn=None)
        with pytest.raises(RuntimeError, match="不可用"):
            eb.add(MemoryEntry(entry_id="x", stock_code="", stock_name="", content="test"))

    def test_add_generates_entry_id_when_empty(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entry = MemoryEntry(entry_id="", stock_code="000001", stock_name="test", content="test")
        eid = eb.add(entry)
        assert eid
        assert eid.startswith("000001_")

    def test_add_preserves_existing_entry_id(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entry = MemoryEntry(entry_id="my_id", stock_code="000001", stock_name="test", content="test")
        eid = eb.add(entry)
        assert eid == "my_id"

    def test_add_calls_embedding_fn_with_content(self, mock_chromadb, mock_embedding_fn, sample_entry):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        eb.add(sample_entry)
        # embedding_fn should have been called with the content text
        # (we can verify indirectly: the upsert was called with a vector)
        call_args = mock_chromadb["collection"].upsert.call_args
        embeddings = call_args[1]["embeddings"]
        assert len(embeddings) == 1
        assert len(embeddings[0]) == 16  # our mock returns 16-dim

    def test_add_upserts_to_collection_with_correct_args(self, mock_chromadb, mock_embedding_fn, sample_entry):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        eb.add(sample_entry)
        mock_chromadb["collection"].upsert.assert_called_once()
        call_kwargs = mock_chromadb["collection"].upsert.call_args[1]
        assert call_kwargs["ids"] == [sample_entry.entry_id]
        assert len(call_kwargs["embeddings"]) == 1
        assert len(call_kwargs["documents"]) == 1
        assert call_kwargs["documents"][0] == sample_entry.content

    def test_add_serializes_tags_to_json_string(self, mock_chromadb, mock_embedding_fn, sample_entry):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        eb.add(sample_entry)
        call_kwargs = mock_chromadb["collection"].upsert.call_args[1]
        tags_val = call_kwargs["metadatas"][0]["tags"]
        assert isinstance(tags_val, str)
        assert "低估值" in tags_val


# ============================================================
# EmbeddingBackend — 批量写入
# ============================================================

class TestEmbeddingBackendAddBatch:
    """Batch write behavior."""

    def test_add_batch_raises_when_unavailable(self):
        eb = EmbeddingBackend(embedding_fn=None)
        with pytest.raises(RuntimeError, match="不可用"):
            eb.add_batch([MemoryEntry(entry_id="x", stock_code="", stock_name="", content="test")])

    def test_add_batch_returns_empty_for_empty_list(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        assert eb.add_batch([]) == []

    def test_add_batch_upserts_all_in_one_call(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entries = [
            MemoryEntry(entry_id="a", stock_code="000001", stock_name="A", content="content A"),
            MemoryEntry(entry_id="b", stock_code="000002", stock_name="B", content="content B"),
        ]
        ids = eb.add_batch(entries)
        assert ids == ["a", "b"]
        mock_chromadb["collection"].upsert.assert_called_once()
        call_kwargs = mock_chromadb["collection"].upsert.call_args[1]
        assert call_kwargs["ids"] == ["a", "b"]
        assert len(call_kwargs["embeddings"]) == 2
        assert len(call_kwargs["documents"]) == 2

    def test_add_batch_generates_ids_for_empty(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        entries = [
            MemoryEntry(entry_id="", stock_code="000001", stock_name="A", content="c1"),
            MemoryEntry(entry_id="", stock_code="000002", stock_name="B", content="c2"),
        ]
        ids = eb.add_batch(entries)
        assert len(ids) == 2
        assert ids[0].startswith("000001_")
        assert ids[1].startswith("000002_")


# ============================================================
# EmbeddingBackend — 检索
# ============================================================

class TestEmbeddingBackendSearch:
    """Query behavior."""

    def test_search_returns_empty_when_unavailable(self):
        eb = EmbeddingBackend(embedding_fn=None)
        assert eb.search("anything") == []

    def test_search_embeds_query_and_calls_collection(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        eb.search("贵州茅台估值")
        mock_chromadb["collection"].query.assert_called_once()
        call_kwargs = mock_chromadb["collection"].query.call_args[1]
        assert "query_embeddings" in call_kwargs
        assert len(call_kwargs["query_embeddings"]) == 1
        assert len(call_kwargs["query_embeddings"][0]) == 16

    def test_search_returns_memory_entries_with_scores(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].query.return_value = {
            "ids": [["id1", "id2"]],
            "metadatas": [[
                {"stock_code": "000001", "stock_name": "平安银行", "date": "2026-07-01",
                 "sector_l1": "", "sector_l2": "", "pe": 0.0, "roe": 0.0,
                 "volatility": "", "tags": "[]", "sentiment": "", "source_query": ""},
                {"stock_code": "000002", "stock_name": "万科A", "date": "2026-07-02",
                 "sector_l1": "", "sector_l2": "", "pe": 0.0, "roe": 0.0,
                 "volatility": "", "tags": "[]", "sentiment": "", "source_query": ""},
            ]],
            "documents": [["content 1", "content 2"]],
            "distances": [[0.2, 1.4]],
        }
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        results = eb.search("query")
        assert len(results) == 2
        assert results[0].entry_id == "id1"
        assert results[0].stock_code == "000001"
        assert results[0].content == "content 1"
        # score: 1 - 0.2/2 = 0.9
        assert results[0].score == pytest.approx(0.9, abs=0.01)
        # score: 1 - 1.4/2 = 0.3
        assert results[1].score == pytest.approx(0.3, abs=0.01)

    def test_search_deserializes_tags(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].query.return_value = {
            "ids": [["id1"]],
            "metadatas": [[{"stock_code": "", "stock_name": "", "date": "",
                            "sector_l1": "", "sector_l2": "", "pe": 0.0, "roe": 0.0,
                            "volatility": "", "tags": '["低估值","龙头"]', "sentiment": "",
                            "source_query": ""}]],
            "documents": [["test"]],
            "distances": [[0.5]],
        }
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        results = eb.search("query")
        assert results[0].metadata["tags"] == ["低估值", "龙头"]

    def test_search_empty_ids_returns_empty(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].query.return_value = {
            "ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]
        }
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        assert eb.search("query") == []


# ============================================================
# EmbeddingBackend — 管理与清理
# ============================================================

class TestEmbeddingBackendDelete:
    def test_delete_returns_false_when_unavailable(self):
        eb = EmbeddingBackend(embedding_fn=None)
        assert eb.delete("any") is False

    def test_delete_calls_collection_delete(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        eb.delete("my_id")
        mock_chromadb["collection"].delete.assert_called_once_with(ids=["my_id"])

    def test_delete_returns_true_on_success(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        assert eb.delete("my_id") is True

    def test_search_excludes_expired_and_superseded_entries(self, mock_chromadb, mock_embedding_fn):
        active = MemoryEntry(
            entry_id="active", stock_code="", stock_name="", content="active",
            expires_at="2099-01-01T00:00:00",
        )
        expired = MemoryEntry(
            entry_id="expired", stock_code="", stock_name="", content="expired",
            expires_at="2020-01-01T00:00:00",
        )
        superseded = MemoryEntry(
            entry_id="superseded", stock_code="", stock_name="", content="superseded",
            superseded_by="new-id",
        )
        entries = [active, expired, superseded]
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        mock_chromadb["collection"].query.return_value = {
            "ids": [[entry.entry_id for entry in entries]],
            "metadatas": [[eb._build_chroma_metadata(entry) for entry in entries]],
            "documents": [[entry.content for entry in entries]],
            "distances": [[0.1, 0.1, 0.1]],
        }

        results = eb.search("query", top_k=3)

        assert [entry.entry_id for entry in results] == ["active"]


class TestEmbeddingBackendCount:
    def test_count_returns_zero_when_unavailable(self):
        assert EmbeddingBackend(embedding_fn=None).count() == 0

    def test_count_delegates_to_collection(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].count.return_value = 42
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        assert eb.count() == 42


class TestEmbeddingBackendClean:
    def test_clean_before_uses_date_filter(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].count.side_effect = [10, 3]
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        deleted = eb.clean_before("2026-01-01")
        assert deleted == 7
        mock_chromadb["collection"].delete.assert_called_once_with(
            where={"date": {"$lt": "2026-01-01"}}
        )

    def test_clean_before_returns_zero_when_unavailable(self):
        assert EmbeddingBackend(embedding_fn=None).clean_before("2026-01-01") == 0

    def test_clean_by_stock_uses_stock_code_filter(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].count.side_effect = [5, 2]
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        deleted = eb.clean_by_stock("000001")
        assert deleted == 3
        mock_chromadb["collection"].delete.assert_called_once_with(
            where={"stock_code": "000001"}
        )


class TestEmbeddingBackendStats:
    def test_stats_returns_unavailable_when_not_ready(self):
        stats = EmbeddingBackend(embedding_fn=None).stats()
        assert stats == {"backend": "embedding", "available": False}

    def test_stats_returns_zero_count(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].count.return_value = 0
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        stats = eb.stats()
        assert stats["semantic_count"] == 0
        assert stats["top_stocks"] == []

    def test_stats_aggregates_top_stocks(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].count.return_value = 3
        mock_chromadb["collection"].get.return_value = {
            "metadatas": [
                {"stock_code": "000001", "stock_name": "平安银行", "sector_l2": "银行", "date": "2026-06-01"},
                {"stock_code": "000001", "stock_name": "平安银行", "sector_l2": "银行", "date": "2026-06-02"},
                {"stock_code": "000002", "stock_name": "万科A", "sector_l2": "地产", "date": "2026-06-03"},
            ]
        }
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        stats = eb.stats()
        assert len(stats["top_stocks"]) == 2
        assert stats["top_stocks"][0] == {"code": "000001", "name": "平安银行", "count": 2}
        assert stats["top_sectors"] == [{"sector": "银行", "count": 2}, {"sector": "地产", "count": 1}]
        assert stats["oldest_entry"] == "2026-06-01"
        assert stats["newest_entry"] == "2026-06-03"


class TestEmbeddingBackendListAll:
    def test_list_all_returns_empty_when_unavailable(self):
        assert EmbeddingBackend(embedding_fn=None).list_all() == []

    def test_list_all_passes_limit_and_offset(self, mock_chromadb, mock_embedding_fn):
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        eb.list_all(limit=10, offset=20)
        mock_chromadb["collection"].get.assert_called_once_with(
            limit=10, offset=20,
            include=["metadatas", "documents"],
        )

    def test_list_all_deserializes_entries(self, mock_chromadb, mock_embedding_fn):
        mock_chromadb["collection"].get.return_value = {
            "ids": ["id1"],
            "metadatas": [{"stock_code": "000001", "stock_name": "test", "date": "",
                           "sector_l1": "", "sector_l2": "", "pe": 0.0, "roe": 0.0,
                           "volatility": "", "tags": '["tag1"]', "sentiment": "",
                           "source_query": ""}],
            "documents": ["content here"],
        }
        eb = EmbeddingBackend(embedding_fn=mock_embedding_fn)
        results = eb.list_all()
        assert len(results) == 1
        assert results[0].entry_id == "id1"
        assert results[0].content == "content here"
        assert results[0].metadata["tags"] == ["tag1"]


# ============================================================
# HybridBackend — 集成
# ============================================================

class TestHybridBackendWithEmbedding:
    """Integration points between HybridBackend and EmbeddingBackend."""

    @pytest.fixture
    def fts5_mock(self):
        fts5 = MagicMock(spec=FTS5Backend)
        fts5.name.return_value = "fts5"
        fts5.is_available.return_value = True
        fts5.add.return_value = "fts5_id"
        fts5.search.return_value = []
        fts5.delete.return_value = True
        fts5.count.return_value = 10
        fts5.clean_before.return_value = 5
        fts5.clean_by_stock.return_value = 3
        fts5.stats.return_value = {"backend": "fts5", "semantic_count": 10}
        fts5.list_all.return_value = []
        fts5.add_batch.return_value = ["a", "b"]
        fts5.is_active.return_value = True
        return fts5

    @pytest.fixture
    def emb_mock(self):
        emb = MagicMock(spec=EmbeddingBackend)
        emb.name.return_value = "embedding"
        emb.is_available.return_value = True
        emb.add.return_value = "emb_id"
        emb.search.return_value = []
        emb.delete.return_value = True
        emb.count.return_value = 10
        emb.clean_before.return_value = 5
        emb.clean_by_stock.return_value = 3
        emb.add_batch.return_value = ["a", "b"]
        return emb

    def test_hybrid_name_is_hybrid(self, fts5_mock, emb_mock):
        hb = HybridBackend(fts5_mock, emb_mock)
        assert hb.name() == "hybrid"

    def test_hybrid_add_writes_to_both(self, fts5_mock, emb_mock, sample_entry):
        hb = HybridBackend(fts5_mock, emb_mock)
        eid = hb.add(sample_entry)
        fts5_mock.add.assert_called_once_with(sample_entry)
        emb_mock.add.assert_called_once_with(sample_entry)
        assert eid == "fts5_id"

    def test_hybrid_add_handles_embedding_error(self, fts5_mock, emb_mock, sample_entry):
        emb_mock.add.side_effect = RuntimeError("boom")
        hb = HybridBackend(fts5_mock, emb_mock)
        eid = hb.add(sample_entry)
        assert eid == "fts5_id"  # should still return FTS5 id

    def test_hybrid_add_skips_unavailable_embedding(self, fts5_mock, emb_mock, sample_entry):
        emb_mock.is_available.return_value = False
        hb = HybridBackend(fts5_mock, emb_mock)
        hb.add(sample_entry)
        emb_mock.add.assert_not_called()

    def test_hybrid_delete_removes_from_both(self, fts5_mock, emb_mock):
        hb = HybridBackend(fts5_mock, emb_mock)
        assert hb.delete("id1") is True
        fts5_mock.delete.assert_called_once_with("id1")
        emb_mock.delete.assert_called_once_with("id1")

    def test_hybrid_clean_before_propagates(self, fts5_mock, emb_mock):
        hb = HybridBackend(fts5_mock, emb_mock)
        result = hb.clean_before("2026-01-01")
        assert result == 5
        emb_mock.clean_before.assert_called_once_with("2026-01-01")

    def test_hybrid_clean_by_stock_propagates(self, fts5_mock, emb_mock):
        hb = HybridBackend(fts5_mock, emb_mock)
        result = hb.clean_by_stock("000001")
        assert result == 3
        emb_mock.clean_by_stock.assert_called_once_with("000001")

    def test_hybrid_stats_includes_embedding_info(self, fts5_mock, emb_mock):
        emb_mock.stats.return_value = {"semantic_count": 10, "db_size_mb": 0}
        hb = HybridBackend(fts5_mock, emb_mock)
        stats = hb.stats()
        assert stats["backend"] == "hybrid"
        assert stats["embedding_available"] is True
        assert stats["embedding_count"] == 10

    def test_hybrid_add_batch_propagates(self, fts5_mock, emb_mock):
        hb = HybridBackend(fts5_mock, emb_mock)
        entries = [MemoryEntry(entry_id="a", stock_code="", stock_name="", content="x")]
        ids = hb.add_batch(entries)
        fts5_mock.add_batch.assert_called_once_with(entries)
        emb_mock.add_batch.assert_called_once_with(entries)
        assert ids == ["a", "b"]

    def test_hybrid_filters_orphan_embedding_candidates(self, fts5_mock, emb_mock):
        orphan = MemoryEntry(entry_id="orphan", stock_code="", stock_name="", content="orphan")
        emb_mock.search.return_value = [orphan]
        fts5_mock.search.return_value = []
        fts5_mock.is_active.return_value = False

        results = HybridBackend(fts5_mock, emb_mock).search("query")

        assert results == []

    def test_hybrid_lifecycle_operations_propagate(self, fts5_mock, emb_mock):
        fts5_mock.mark_superseded.return_value = 2
        fts5_mock.clean_expired.return_value = 3
        hb = HybridBackend(fts5_mock, emb_mock)

        assert hb.mark_superseded(["a", "b"], "new") == 2
        assert hb.clean_expired("2026-07-17T00:00:00") == 3
        emb_mock.mark_superseded.assert_called_once_with(
            ["a", "b"], "new", reason="new_analysis"
        )
        emb_mock.clean_expired.assert_called_once_with("2026-07-17T00:00:00")

    # -- RRF fusion tests --

    def test_rrf_fusion_combines_both_rankings(self, fts5_mock, emb_mock):
        """When both backends return results, RRF combines and re-ranks them."""
        e1 = MemoryEntry(entry_id="e1", stock_code="s1", stock_name="n1", content="c1", score=0.0)
        e2 = MemoryEntry(entry_id="e2", stock_code="s2", stock_name="n2", content="c2", score=0.0)
        e3 = MemoryEntry(entry_id="e3", stock_code="s3", stock_name="n3", content="c3", score=0.0)
        fts5_mock.search.return_value = [e1, e2]
        emb_mock.search.return_value = [e2, e3]

        hb = HybridBackend(fts5_mock, emb_mock)
        results = hb.search("query", top_k=5)

        # e2 appears in both -> highest RRF score
        # e1 only FTS5 pos 0, e3 only EMB pos 1 -> e1 has higher RRF
        # Order: e2, e1, e3
        assert len(results) == 3
        assert results[0].entry_id == "e2"
        assert results[1].entry_id == "e1"
        assert results[2].entry_id == "e3"
        # scores should be reassigned
        for r in results:
            assert r.score > 0.0

    def test_rrf_falls_back_to_fts5_when_embedding_unavailable(self, fts5_mock, emb_mock):
        emb_mock.is_available.return_value = False
        e1 = MemoryEntry(entry_id="e1", stock_code="s1", stock_name="n1", content="c1")
        fts5_mock.search.return_value = [e1]

        hb = HybridBackend(fts5_mock, emb_mock)
        results = hb.search("query")
        assert results == [e1]
        emb_mock.search.assert_not_called()

    def test_rrf_falls_back_when_embedding_errors(self, fts5_mock, emb_mock):
        emb_mock.search.side_effect = RuntimeError("boom")
        e1 = MemoryEntry(entry_id="e1", stock_code="s1", stock_name="n1", content="c1")
        fts5_mock.search.return_value = [e1]

        hb = HybridBackend(fts5_mock, emb_mock)
        results = hb.search("query")
        assert results == [e1]

    def test_rrf_respects_top_k(self, fts5_mock, emb_mock):
        entries = [
            MemoryEntry(entry_id=f"e{i}", stock_code="s", stock_name="n", content="c")
            for i in range(20)
        ]
        fts5_mock.search.return_value = entries[:10]
        emb_mock.search.return_value = entries[10:]

        hb = HybridBackend(fts5_mock, emb_mock)
        results = hb.search("query", top_k=5)
        assert len(results) == 5


# ============================================================
# 工厂函数
# ============================================================

class TestCreateMemoryBackend:
    """Factory function behavior."""

    def test_fts5_mode_returns_fts5(self):
        backend = create_memory_backend(mode="fts5", embedding_fn=None)
        assert backend.name() == "fts5"
        assert isinstance(backend, FTS5Backend)

    def test_embedding_mode_raises_without_fn(self):
        with pytest.raises(ValueError, match="embedding_fn"):
            create_memory_backend(mode="embedding", embedding_fn=None)

    def test_auto_mode_returns_fts5_when_no_embedding_fn(self):
        backend = create_memory_backend(mode="auto", embedding_fn=None)
        assert backend.name() == "fts5"

    def test_auto_mode_returns_hybrid_when_embedding_fn_available(self, mock_chromadb, mock_embedding_fn):
        backend = create_memory_backend(mode="auto", embedding_fn=mock_embedding_fn)
        assert backend.name() == "hybrid"
        assert isinstance(backend, HybridBackend)

    def test_hybrid_mode_returns_hybrid_when_available(self, mock_chromadb, mock_embedding_fn):
        backend = create_memory_backend(mode="hybrid", embedding_fn=mock_embedding_fn)
        assert backend.name() == "hybrid"

    def test_hybrid_mode_falls_back_to_fts5_when_not_available(self):
        backend = create_memory_backend(mode="hybrid", embedding_fn=None)
        assert backend.name() == "fts5"

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="未知 backend mode"):
            create_memory_backend(mode="unknown")


# ============================================================
# Embedding 服务就绪探测（初始化时只读探针 + 超时降级）
# ============================================================

class TestEmbeddingReadinessProbe:
    """_wait_embedding_ready：只探测不写数据，超时降级。"""

    def test_ready_immediately_when_fn_returns_valid_vector(self):
        from memory.backend import _wait_embedding_ready
        fn = lambda t: [0.1, 0.2, 0.3]
        # 首次即成功，不应有任何轮询等待
        assert _wait_embedding_ready(fn, timeout=2.0, interval=0.1) is True

    def test_ready_when_fn_matches_real_embedding_shape(self, mock_embedding_fn):
        from memory.backend import _wait_embedding_ready
        # 真实形态的 embedding_fn（md5 -> 16 维向量）应被判定为就绪
        assert _wait_embedding_ready(mock_embedding_fn, timeout=2.0, interval=0.1) is True

    def test_returns_false_after_timeout_when_fn_always_raises(self, mock_chromadb):
        from memory.backend import _wait_embedding_ready
        # 模拟 Ollama 未启动：连接被拒，embedding_fn 持续抛异常
        def _raise(text):
            raise ConnectionError("Ollama connection refused")
        # 短超时验证轮询耗尽后返回 False（不抛异常、不写数据）
        assert _wait_embedding_ready(_raise, timeout=0.8, interval=0.1) is False

    def test_no_probe_data_written_when_not_ready(self, mock_chromadb):
        """超时降级时不得向 chroma 写入任何验证数据。"""
        from memory.backend import _wait_embedding_ready
        def _raise(text):
            raise ConnectionError("down")
        _wait_embedding_ready(_raise, timeout=0.4, interval=0.1)
        # 探针从不调用 collection.upsert（仅调用 embedding_fn），故 0 次写入
        mock_chromadb["collection"].upsert.assert_not_called()


class TestFTS5BackendDedup:
    """FTS5Backend.add 必须按 entry_id 去重（FTS5 无 UNIQUE 约束，
    旧实现 INSERT OR REPLACE 会追加重复行）。"""

    def test_add_same_entry_id_produces_single_fts_row(self, tmp_path):
        from memory.backend import get_stock_memory_db_path
        db = str(tmp_path / "fts_dedup.db")
        b = FTS5Backend(db)
        entry = MemoryEntry(
            entry_id="abc_20260706_x", stock_code="603986", stock_name="韦尔股份",
            content="测试内容", metadata={"source_query": "x", "date": "2026-07-06T00:00:00"},
        )
        b.add(entry)
        b.add(entry)  # 重复写入同一 entry_id
        conn = b._get_conn()
        fts_cnt = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory_fts WHERE entry_id=?", ("abc_20260706_x",)
        ).fetchone()[0]
        meta_cnt = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory_meta WHERE entry_id=?", ("abc_20260706_x",)
        ).fetchone()[0]
        assert fts_cnt == 1, f"FTS5 重复行: {fts_cnt}"
        assert meta_cnt == 1


class TestCreateMemoryBackendReadiness:
    """create_memory_backend 初始化行为测试。

    hybrid/auto 模式：不再阻塞等待 embedding 就绪，直接返回 HybridBackend。
    容错由 HybridBackend 自身完成（add()/search() 时降级）。
    只有纯 embedding 模式仍需在初始化时确认服务可用。
    """

    def test_auto_returns_hybrid_regardless_of_service_status(self, mock_chromadb):
        """auto 模式：chromadb 已安装 → 直接返回 HybridBackend（不探测服务）"""
        def _raise(text):
            raise ConnectionError("Ollama down")
        backend = create_memory_backend(
            mode="auto", embedding_fn=_raise,
            embedding_ready_timeout=0.8, embedding_ready_interval=0.1,
        )
        assert backend.name() == "hybrid"
        assert isinstance(backend, HybridBackend)

    def test_auto_returns_hybrid_when_embedding_service_ready(self, mock_chromadb, mock_embedding_fn):
        backend = create_memory_backend(
            mode="auto", embedding_fn=mock_embedding_fn,
            embedding_ready_timeout=2.0, embedding_ready_interval=0.1,
        )
        assert backend.name() == "hybrid"
        assert isinstance(backend, HybridBackend)

    def test_embedding_mode_raises_when_service_down(self, mock_chromadb):
        """纯 embedding 模式仍需确认服务就绪"""
        def _raise(text):
            raise ConnectionError("Ollama down")
        with pytest.raises(RuntimeError, match="未就绪"):
            create_memory_backend(
                mode="embedding", embedding_fn=_raise,
                embedding_ready_timeout=0.8, embedding_ready_interval=0.1,
            )

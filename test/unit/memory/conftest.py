"""Test fixtures for memory backend unit tests."""
import sys
import os

# 测试环境禁用 reranker（避免 HuggingFace 下载超时）
os.environ["STOCK_MEMORY_RERANKER"] = ""

# Ensure project root is in sys.path (needed for pytest >= 9)
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Fake chromadb — since chromadb isn't installed in this environment,
# we inject a mock into sys.modules so that `import chromadb` in
# EmbeddingBackend.__init__ resolves successfully.
# ---------------------------------------------------------------------------

def _build_fake_chromadb():
    """Build a fake chromadb module with PersistentClient."""
    fake_collection = MagicMock()
    fake_collection.count.return_value = 0
    fake_collection.query.return_value = {
        "ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]
    }
    fake_collection.get.return_value = {
        "ids": [], "metadatas": [], "documents": []
    }

    fake_client = MagicMock()
    fake_client.get_or_create_collection.return_value = fake_collection

    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient = MagicMock(return_value=fake_client)

    return fake_chromadb, fake_client, fake_collection


@pytest.fixture
def mock_chromadb():
    """Inject a fake chromadb module into sys.modules for the test duration."""
    fake_chromadb, fake_client, fake_collection = _build_fake_chromadb()

    # Inject into sys.modules so `import chromadb` works
    sys.modules["chromadb"] = fake_chromadb

    yield {
        "module": fake_chromadb,
        "client_class": fake_chromadb.PersistentClient,
        "client": fake_client,
        "collection": fake_collection,
    }

    # Clean up
    sys.modules.pop("chromadb", None)


@pytest.fixture
def mock_embedding_fn():
    """A deterministic pseudo-embedding function (MD5 hash -> 16-dim vector)."""
    import hashlib

    def _embed(text: str) -> list:
        h = hashlib.md5(text.encode()).digest()
        return [float(b) / 255.0 for b in h[:16]]

    return _embed


@pytest.fixture
def sample_entry():
    """A representative MemoryEntry for testing."""
    from memory.backend import MemoryEntry
    return MemoryEntry(
        entry_id="test_001",
        stock_code="000001",
        stock_name="平安银行",
        content="估值分析：PE处于历史低位",
        metadata={
            "date": "2026-07-01",
            "sector_l1": "金融",
            "sector_l2": "银行",
            "pe": 5.2,
            "tags": ["低估值", "银行股"],
            "sentiment": "bullish",
        }
    )

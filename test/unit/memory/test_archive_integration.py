from datetime import datetime, timedelta
from unittest.mock import MagicMock

from memory.backend.base import MemoryEntry
from memory.backend.fts5 import FTS5Backend
from memory.sdk import MemoryConfig, MemorySDK


class _ArchiveBackend:
    def __init__(self, old_entry):
        self.entries = [old_entry]
        self.events = []

    def get_by_stock(self, stock_code, limit=20):
        self.events.append("get")
        return list(self.entries)

    def add_batch(self, entries):
        self.events.append("add")
        for index, entry in enumerate(entries, 1):
            entry.entry_id = f"new-{index}"
            self.entries.append(entry)
        return [entry.entry_id for entry in entries]

    def add(self, entry):
        return self.add_batch([entry])[0]

    def mark_superseded(self, entry_ids, new_entry_id, reason="new_analysis"):
        self.events.append(("supersede", list(entry_ids), new_entry_id))
        count = 0
        for entry in self.entries:
            if entry.entry_id in entry_ids:
                entry.superseded_by = new_entry_id
                entry.supersede_reason = reason
                count += 1
        return count

    def name(self):
        return "fake"

    def count(self):
        return len(self.entries)


def test_archive_queries_old_entries_before_write_and_patches_only_old_ids(monkeypatch):
    import memory.sdk as sdk_module

    old = MemoryEntry(
        entry_id="old-1",
        stock_code="000001",
        stock_name="平安银行",
        content="旧结论",
        metadata={
            "subject_kind": "stock",
            "date": (datetime.now() - timedelta(days=10)).isoformat(),
        },
        confidence=0.91,
        provenance={"source": "old-provider"},
        ttl_days=365,
    )
    backend = _ArchiveBackend(old)
    sdk = MemorySDK(MemoryConfig(memory_db_path=""))
    sdk._backend = backend
    sdk._initialized = True

    monkeypatch.setattr(
        sdk_module,
        "collect_metadata",
        lambda user_input, result: {
            "stock_code": "000001",
            "stock_name": "平安银行",
            "subject_kind": "stock",
            "source_query": user_input,
        },
    )
    monkeypatch.setattr(sdk_module, "chunk_report", lambda text: ["新片段一", "新片段二"])
    monkeypatch.setattr(sdk_module, "strip_report_decor", lambda text: text)
    monkeypatch.setattr(sdk_module, "_reset_metadata", lambda: None)
    monkeypatch.setattr(sdk_module.EpisodicMemory, "log", lambda self, **kwargs: None)

    sdk.archive("分析平安银行", "新结论", logger=MagicMock())

    assert backend.events[0] == "get"
    assert backend.events[1] == "add"
    assert backend.events[2] == ("supersede", ["old-1"], "new-1")
    assert old.superseded_by == "new-1"
    assert old.confidence == 0.91
    assert old.provenance == {"source": "old-provider"}
    assert old.ttl_days == 365
    assert [entry.superseded_by for entry in backend.entries[1:]] == ["", ""]
    assert all(entry.metadata["subject_kind"] == "stock" for entry in backend.entries[1:])


def test_fts_get_by_stock_roundtrips_lifecycle_and_patch_preserves_payload(tmp_path):
    backend = FTS5Backend(str(tmp_path / "memory.db"))
    entry = MemoryEntry(
        entry_id="old-1",
        stock_code="000001",
        stock_name="平安银行",
        content="完整旧结论",
        metadata={
            "date": "2026-07-01T00:00:00",
            "source_query": "旧问题",
            "subject_kind": "stock",
        },
        confidence=0.91,
        confidence_factors={"base": 0.9},
        provenance={"source": "provider-a"},
        memory_category="fundamental",
        ttl_days=365,
        expires_at="2027-07-01T00:00:00",
        last_validated_at="2026-07-02T00:00:00",
    )
    backend.add(entry)

    before = backend.get_by_stock("000001")[0]
    assert before.metadata["subject_kind"] == "stock"
    assert before.confidence == 0.91
    assert before.provenance == {"source": "provider-a"}
    assert before.memory_category == "fundamental"
    assert before.ttl_days == 365

    assert backend.mark_superseded(["old-1"], "new-1") == 1
    after = backend.get_by_stock("000001")[0]
    assert after.superseded_by == "new-1"
    assert after.confidence == 0.91
    assert after.provenance == {"source": "provider-a"}
    assert after.ttl_days == 365


def test_fts_initialization_repairs_historical_self_supersede(tmp_path):
    db_path = str(tmp_path / "memory.db")
    first = FTS5Backend(db_path)
    first.add(MemoryEntry(
        entry_id="self-1",
        stock_code="000001",
        stock_name="平安银行",
        content="旧错误记录",
        metadata={"date": "2026-07-01T00:00:00", "subject_kind": "stock"},
        superseded_by="self-1",
        superseded_at="2026-07-02T00:00:00",
        supersede_reason="new_analysis",
    ))

    repaired_backend = FTS5Backend(db_path)
    repaired = repaired_backend._get_conn().execute(
        "SELECT superseded_by, superseded_at, supersede_reason "
        "FROM semantic_memory_meta WHERE entry_id = ?",
        ("self-1",),
    ).fetchone()

    assert repaired == (None, None, None)

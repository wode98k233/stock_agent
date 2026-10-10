"""chunk 元数据（chunk_index/chunk_count/chunk_group_id）落库 + 迁移 + 回填 单测。

背景（2026-09-23）：archive 一直把三字段写进 MemoryEntry.metadata，但 FTS5/Chroma
此前不持久化，导致 retrieve 的同报告分组合并 + 文档序失效。本组测试锁定修复。
"""
import os
import sqlite3

from memory.backend.base import MemoryEntry
from memory.backend.fts5 import FTS5Backend
from memory.backend.embedding import EmbeddingBackend
from memory.sdk import MemorySDK, MemoryConfig


# ── 迁移：旧表无 chunk 列 → ensure_schema 加性补列 ─────────────

_OLD_META_DDL = """CREATE TABLE IF NOT EXISTS semantic_memory_meta (
    entry_id        TEXT PRIMARY KEY,
    stock_code      TEXT NOT NULL,
    stock_name      TEXT NOT NULL,
    sector_l1       TEXT,
    sector_l2       TEXT,
    pe              REAL,
    roe             REAL,
    volatility      TEXT,
    tags            TEXT,
    sentiment       TEXT,
    source_query    TEXT,
    date            TEXT NOT NULL,
    access_count    INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
)"""


def test_migration_adds_chunk_columns(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(_OLD_META_DDL)
    conn.execute("INSERT INTO semantic_memory_meta (entry_id, stock_code, stock_name, date) "
                 "VALUES ('e1', '002714', '牧原', '2026-09-23')")
    conn.commit()
    conn.close()

    FTS5Backend(db)  # 触发 ensure_schema（加性迁移）

    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(semantic_memory_meta)")]
    conn.close()
    assert "chunk_index" in cols
    assert "chunk_count" in cols
    assert "chunk_group_id" in cols


# ── FTS5 roundtrip ─────────────────────────────────────────

def test_fts5_chunk_metadata_roundtrip(tmp_path):
    be = FTS5Backend(str(tmp_path / "rt.db"))
    be.add(MemoryEntry(
        entry_id="", stock_code="002714", stock_name="牧原股份",
        content="alpha report body",
        metadata={"date": "2026-09-23T19:00:00", "source_query": "q",
                  "chunk_index": 3, "chunk_count": 7, "chunk_group_id": "g77"},
        confidence=0.9,
    ))
    hits = be.search("alpha", top_k=5)
    assert hits, "应命中"
    m = hits[0].metadata
    assert (m["chunk_index"], m["chunk_count"], m["chunk_group_id"]) == (3, 7, "g77")
    # get_all_raw 同步带回
    raw = be.get_all_raw()
    assert (raw[0]["chunk_index"], raw[0]["chunk_count"], raw[0]["chunk_group_id"]) == (3, 7, "g77")
    be.close()


def test_chroma_metadata_roundtrip():
    e = MemoryEntry(entry_id="e", stock_code="x", stock_name="x", content="c",
                    metadata={"date": "2026-09-23", "tags": [],
                              "chunk_index": 4, "chunk_count": 9, "chunk_group_id": "gg"},
                    confidence=0.8)
    emb = EmbeddingBackend.__new__(EmbeddingBackend)
    cm = EmbeddingBackend._build_chroma_metadata(emb, e)
    parsed, _, _ = EmbeddingBackend._parse_chroma_metadata(cm)
    assert (parsed["chunk_index"], parsed["chunk_count"], parsed["chunk_group_id"]) == (4, 9, "gg")


# ── 回填 ───────────────────────────────────────────────────

def _sdk(tmp_path):
    return MemorySDK(MemoryConfig(backend_mode="fts5",
                                  memory_db_path=str(tmp_path / "bk.db")))


def test_backfill_assigns_document_order_and_is_idempotent(tmp_path):
    sdk = _sdk(tmp_path)
    sdk._ensure_init()
    for i in range(3):
        sdk._backend.add(MemoryEntry(
            entry_id=f"e{i}", stock_code="002714", stock_name="牧原股份",
            content=f"body {i}",
            metadata={"date": "2026-09-23T19:00:00", "source_query": "同一问题", "tags": []},
            confidence=0.9,
        ))

    r1 = sdk.backfill_chunk_metadata()
    assert r1["checked"] == 3 and r1["updated"] == 3

    rows = sdk._backend.get_all_raw()
    by_id = {r["entry_id"]: r for r in rows}
    idxs = sorted(by_id[f"e{i}"]["chunk_index"] for i in range(3))
    assert idxs == [0, 1, 2]
    gids = {by_id[f"e{i}"]["chunk_group_id"] for i in range(3)}
    assert len(gids) == 1 and next(iter(gids))  # 同组、非空
    assert all(by_id[f"e{i}"]["chunk_count"] == 3 for i in range(3))

    # 幂等：二次运行不再更新
    assert sdk.backfill_chunk_metadata()["updated"] == 0

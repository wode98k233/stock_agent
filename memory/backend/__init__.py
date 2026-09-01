"""
检索后端策略体系 — 数据模型、三种后端实现、Reranker 和工厂函数。
通过包暴露，外部 import 路径不变：`from memory.backend import MemoryBackend, ...`
"""
from memory.backend.base import MemoryEntry, MemoryBackend
from memory.backend.fts5 import FTS5Backend, ensure_jieba_ready
from memory.backend.embedding import (
    EmbeddingBackend,
    _wait_embedding_ready,
    EMBEDDING_READY_TIMEOUT,
    EMBEDDING_READY_INTERVAL,
)
from memory.backend.hybrid import (
    Reranker,
    HybridBackend,
    create_memory_backend,
)
from utils.app_paths import get_stock_memory_db_path  # 向后兼容：旧代码从 memory.backend import 此函数

__all__ = [
    "MemoryEntry",
    "MemoryBackend",
    "FTS5Backend",
    "EmbeddingBackend",
    "Reranker",
    "HybridBackend",
    "create_memory_backend",
    "_wait_embedding_ready",
    "EMBEDDING_READY_TIMEOUT",
    "EMBEDDING_READY_INTERVAL",
    "get_stock_memory_db_path",
    "ensure_jieba_ready",
]

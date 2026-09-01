"""
记忆系统 — Embedding 后端（ChromaDB 向量检索，可选）
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import List, Optional, Callable

from utils.app_paths import get_chroma_data_path
from memory.backend.base import MemoryEntry, MemoryBackend

_log = logging.getLogger(__name__)


# ------------------------------------------------------------
# Embedding 服务就绪探测（初始化时调用，不写入任何数据）
# ------------------------------------------------------------
EMBEDDING_READY_TIMEOUT = 30.0   # 最长等待秒数
EMBEDDING_READY_INTERVAL = 1.5   # 轮询间隔秒数
EMBEDDING_PROBE_TEXT = "__memory_readiness_probe__"  # 探测用占位文本，绝不写入持久化存储


def _wait_embedding_ready(embedding_fn, timeout: float = EMBEDDING_READY_TIMEOUT,
                           interval: float = EMBEDDING_READY_INTERVAL,
                           logger=None, fast_fail_on_connection_error: bool = False) -> bool:
    """探测 embedding 服务是否就绪（只调用 embedding_fn，不写任何数据）。

    就绪判定：embedding_fn 返回一个非空、元素均为数值的向量。
    服务冷启动 / 暂时不可达时按 interval 轮询，直到 timeout。
    超时返回 False，由调用方决定降级策略（纯 FTS5）。

    fast_fail_on_connection_error=True 时，若探测到连接类错误（如 Ollama 未启动），
    立即返回 False 而不空等 timeout——用于启动期预热：避免 Ollama 没开时把对话
    永久阻塞在 503。
    """
    log = logger or _log
    deadline = time.time() + timeout
    last_err = None
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            vec = embedding_fn(EMBEDDING_PROBE_TEXT)
            if isinstance(vec, (list, tuple)) and len(vec) > 0:
                sample = vec[:min(len(vec), 16)]
                if all(isinstance(x, (int, float)) for x in sample):
                    log.info(
                        "[memory] embedding 服务就绪 (dim=%d, attempts=%d)",
                        len(vec), attempt,
                    )
                    return True
            last_err = (
                f"向量格式异常: type={type(vec).__name__} "
                f"len={len(vec) if hasattr(vec, '__len__') else '?'}"
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if fast_fail_on_connection_error and _is_connection_error(e):
                log.warning(
                    "[memory] embedding 服务连接失败（大概率未启动），直接降级: %s",
                    last_err,
                )
                return False
        time.sleep(interval)
    log.warning(
        "[memory] embedding 服务在 %.0fs 内未就绪，将降级为纯 FTS5（最后一次错误: %s）",
        timeout, last_err,
    )
    return False


def _is_connection_error(e: Exception) -> bool:
    """粗略判断是否为「服务不可达」类错误（连接被拒 / 无法连接 / 名称解析失败）。"""
    name = type(e).__name__
    if "Connection" in name or "Connect" in name or "ConnectTimeout" in name:
        return True
    msg = str(e).lower()
    keywords = ("failed to connect", "connection refused", "connection error",
                "cannot connect", "name or service not known", "nodename nor servname",
                "max retries", "connection aborted", "连接")
    return any(k in msg for k in keywords)


# ------------------------------------------------------------
# Embedding 后端 — ChromaDB 向量检索（可选，需 embedding 模型）
# ------------------------------------------------------------

def _filter_by_min_similarity(entries: List[MemoryEntry],
                              min_similarity: float) -> List[MemoryEntry]:
    """按余弦相似度门槛过滤检索结果。

    min_similarity <= 0 视为不启用门槛（全部保留）。
    返回的条目保持原有顺序（ChromaDB 已按距离升序返回，即相似度降序）。
    """
    if not min_similarity or min_similarity <= 0:
        return entries
    return [e for e in entries if (e.score or 0.0) >= min_similarity]


class EmbeddingBackend(MemoryBackend):
    """基于 ChromaDB 的语义检索后端。

    通过 embedding_fn 将文本转向量，存入 ChromaDB 持久化集合，
    检索时用余弦相似度匹配语义相近的记忆。

    embedding_fn 由 memory/sdk.py 的 _build_embedding_fn() 构建，
    支持 OpenAI API 和本地 sentence-transformers 两种模式。
    """

    def __init__(self, embedding_fn: Optional[Callable] = None,
                 chroma_path: Optional[str] = None,
                 min_similarity: float = 0.0):
        self._embedding_fn = embedding_fn
        self._available = False
        self._client = None
        self._collection = None
        # 语义相似度门槛：score(余弦相似度) < 该值的向量结果会被过滤掉，
        # 避免无关记忆污染检索/注入上下文。0 表示不启用门槛。
        self._min_similarity = float(min_similarity or 0.0)

        if embedding_fn is None:
            return

        try:
            import chromadb
        except ImportError:
            return

        if chroma_path is None:
            chroma_path = get_chroma_data_path()

        self._client = chromadb.PersistentClient(path=chroma_path)
        self._collection = self._client.get_or_create_collection(
            name="stock_memory",
            metadata={"hnsw:space": "cosine"},
        )
        self._available = True

    # ── 属性 ──────────────────────────────────────────────

    def name(self) -> str:
        return "embedding"

    def is_available(self) -> bool:
        return self._available

    def set_min_similarity(self, value: float) -> None:
        """运行时调整语义相似度门槛（0 表示关闭）。"""
        self._min_similarity = float(value or 0.0)

    def get_min_similarity(self) -> float:
        return self._min_similarity

    # ── 元数据序列化 ──────────────────────────────────────

    def _build_chroma_metadata(self, entry: MemoryEntry) -> dict:
        """MemoryEntry.metadata → ChromaDB flat dict（tags 序列化为 JSON string）"""
        meta = entry.metadata or {}
        chroma_meta = {
            "entry_id": entry.entry_id,
            "stock_code": entry.stock_code,
            "stock_name": entry.stock_name,
            "date": meta.get("date", datetime.now().isoformat()),
            "sector_l1": meta.get("sector_l1") or "",
            "sector_l2": meta.get("sector_l2") or "",
            "pe": float(meta["pe"]) if meta.get("pe") is not None else 0.0,
            "roe": float(meta["roe"]) if meta.get("roe") is not None else 0.0,
            "volatility": meta.get("volatility") or "",
            "tags": json.dumps(meta.get("tags") or [], ensure_ascii=False),
            "sentiment": meta.get("sentiment") or "",
            "source_query": meta.get("source_query") or "",
            "subject_kind": meta.get("subject_kind") or "",
            # v2 M1
            "confidence": float(entry.confidence),
            "trace_run_id": entry.provenance.get("trace_run_id", "") if entry.provenance else "",
            "confidence_factors": json.dumps(entry.confidence_factors or {}, ensure_ascii=False),
            "provenance": json.dumps(entry.provenance or {}, ensure_ascii=False),
            # v2 M2：时效性
            "memory_category": entry.memory_category,
            "ttl_days": str(entry.ttl_days),
            "expires_at": entry.expires_at or "",
            "last_validated_at": entry.last_validated_at or "",
            "superseded_by": entry.superseded_by or "",
            "superseded_at": entry.superseded_at or "",
            "supersede_reason": entry.supersede_reason or "",
        }
        return chroma_meta

    @staticmethod
    def _parse_chroma_metadata(chroma_meta: dict) -> tuple:
        """ChromaDB flat dict → (metadata_dict, confidence, trace_run_id)。

        Returns:
            (metadata: dict, confidence: float, trace_run_id: str)
        """
        tags = []
        tags_raw = chroma_meta.get("tags", "")
        if tags_raw:
            try:
                tags = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags = []
        conf = chroma_meta.get("confidence", 0.5)
        if conf is None:
            conf = 0.5
        conf = float(conf)
        meta = {
            "date": chroma_meta.get("date", ""),
            "sector_l1": chroma_meta.get("sector_l1", ""),
            "sector_l2": chroma_meta.get("sector_l2", ""),
            "pe": chroma_meta.get("pe", 0.0),
            "roe": chroma_meta.get("roe", 0.0),
            "volatility": chroma_meta.get("volatility", ""),
            "tags": tags,
            "sentiment": chroma_meta.get("sentiment", ""),
            "source_query": chroma_meta.get("source_query", ""),
            "subject_kind": chroma_meta.get("subject_kind", ""),
        }
        for key in ("confidence_factors", "provenance"):
            try:
                meta[f"_{key}"] = json.loads(chroma_meta.get(key, "") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta[f"_{key}"] = {}
        meta["_lifecycle"] = {
            "memory_category": chroma_meta.get("memory_category", "general") or "general",
            "ttl_days": int(chroma_meta.get("ttl_days", 30) or 30),
            "expires_at": chroma_meta.get("expires_at", "") or "",
            "last_validated_at": chroma_meta.get("last_validated_at", "") or "",
            "superseded_by": chroma_meta.get("superseded_by", "") or "",
            "superseded_at": chroma_meta.get("superseded_at", "") or "",
            "supersede_reason": chroma_meta.get("supersede_reason", "") or "",
        }
        trace_run_id = chroma_meta.get("trace_run_id", "") or ""
        return meta, conf, trace_run_id

    # ── 写入 ──────────────────────────────────────────────

    def add(self, entry: MemoryEntry) -> str:
        if not self._available:
            raise RuntimeError("EmbeddingBackend 不可用：缺少 embedding_fn 或 chromadb 未安装")
        if not entry.entry_id:
            entry.entry_id = (
                f"{entry.stock_code}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                f"{uuid.uuid4().hex[:6]}"
            )
        t_emb = time.time()
        vector = self._embedding_fn(entry.content)
        emb_ms = int((time.time() - t_emb) * 1000)
        _log.debug("[emb] embed elapsed=%dms dim=%d eid=%s", emb_ms, len(vector), entry.entry_id[:12])

        chroma_meta = self._build_chroma_metadata(entry)
        self._collection.upsert(
            ids=[entry.entry_id],
            embeddings=[vector],
            metadatas=[chroma_meta],
            documents=[entry.content],
        )
        return entry.entry_id

    def add_batch(self, entries: List[MemoryEntry]) -> List[str]:
        """批量写入 — 单次 upsert 调用，比逐条写入快 10-50 倍"""
        if not self._available:
            raise RuntimeError("EmbeddingBackend 不可用")
        if not entries:
            return []
        ids, vectors, metadatas, documents = [], [], [], []
        for entry in entries:
            if not entry.entry_id:
                entry.entry_id = (
                    f"{entry.stock_code}_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:6]}"
                )
            ids.append(entry.entry_id)
            vectors.append(self._embedding_fn(entry.content))
            metadatas.append(self._build_chroma_metadata(entry))
            documents.append(entry.content)
        self._collection.upsert(
            ids=ids, embeddings=vectors,
            metadatas=metadatas, documents=documents,
        )
        return ids

    # ── 检索 ──────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5,
               include_inactive: bool = False) -> List[MemoryEntry]:
        if not self._available:
            return []
        t_emb = time.time()
        query_vector = self._embedding_fn(query)
        emb_ms = int((time.time() - t_emb) * 1000)
        results = self._collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            include=["metadatas", "documents", "distances"],
        )
        entries = []
        ids_batch = results.get("ids", [[]])[0]
        if not ids_batch:
            _log.debug("[emb] search no results query=%r elapsed=%dms", query[:60], emb_ms)
            return []
        metas_list = results.get("metadatas", [[]])[0] or []
        docs_list = results.get("documents", [[]])[0] or []
        dists_list = results.get("distances", [[]])[0] or []
        for i, entry_id in enumerate(ids_batch):
            meta = metas_list[i] if i < len(metas_list) else {}
            doc = docs_list[i] if i < len(docs_list) else ""
            dist = dists_list[i] if i < len(dists_list) else 1.0
            # Cosine distance in [0, 2] -> similarity score in [0, 1]
            score = max(0.0, 1.0 - (dist / 2.0))
            parsed_meta, conf, trace_run_id = self._parse_chroma_metadata(meta)
            lifecycle = parsed_meta.pop("_lifecycle")
            confidence_factors = parsed_meta.pop("_confidence_factors")
            provenance = parsed_meta.pop("_provenance")
            if trace_run_id and "trace_run_id" not in provenance:
                provenance["trace_run_id"] = trace_run_id
            entries.append(MemoryEntry(
                entry_id=entry_id,
                stock_code=meta.get("stock_code", ""),
                stock_name=meta.get("stock_name", ""),
                content=doc,
                score=round(score, 4),
                metadata=parsed_meta,
                confidence=conf,
                confidence_factors=confidence_factors,
                provenance=provenance,
                **lifecycle,
            ))
        _log.debug("[emb] search hits=%d query=%r elapsed=%dms", len(entries), query[:60], emb_ms)
        # 语义相似度门槛：过滤掉余弦相似度低于阈值的向量结果，
        # 避免无关记忆污染 RRF 融合与注入上下文。
        entries = _filter_by_min_similarity(entries, self._min_similarity)
        if not include_inactive:
            now = datetime.now().isoformat()
            entries = [
                entry for entry in entries
                if not entry.superseded_by
                and (not entry.expires_at or entry.expires_at > now)
            ]
        return entries[:top_k]

    # ── 管理 ──────────────────────────────────────────────

    def delete(self, entry_id: str) -> bool:
        if not self._available:
            return False
        try:
            self._collection.delete(ids=[entry_id])
            return True
        except Exception:
            return False

    def mark_superseded(self, entry_ids: List[str], new_entry_id: str,
                        reason: str = "new_analysis") -> int:
        if not self._available:
            return 0
        ids = [entry_id for entry_id in entry_ids if entry_id and entry_id != new_entry_id]
        if not ids:
            return 0
        data = self._collection.get(ids=ids, include=["metadatas"])
        found_ids = data.get("ids", []) or []
        metadatas = data.get("metadatas", []) or []
        now = datetime.now().isoformat()
        updated = []
        for index, _ in enumerate(found_ids):
            metadata = dict(metadatas[index] if index < len(metadatas) else {})
            metadata.update({
                "superseded_by": new_entry_id,
                "superseded_at": now,
                "supersede_reason": reason,
            })
            updated.append(metadata)
        if found_ids:
            self._collection.update(ids=found_ids, metadatas=updated)
        return len(found_ids)

    def clean_expired(self, as_of: str | None = None) -> int:
        if not self._available:
            return 0
        as_of = as_of or datetime.now().isoformat()
        data = self._collection.get(include=["metadatas"])
        ids = data.get("ids", []) or []
        metadatas = data.get("metadatas", []) or []
        expired_ids = [
            entry_id
            for index, entry_id in enumerate(ids)
            if index < len(metadatas)
            and metadatas[index].get("expires_at")
            and metadatas[index]["expires_at"] <= as_of
        ]
        if expired_ids:
            self._collection.delete(ids=expired_ids)
        return len(expired_ids)

    def count(self) -> int:
        if not self._available:
            return 0
        return self._collection.count()

    def clean_before(self, date: str) -> int:
        if not self._available:
            return 0
        before = self._collection.count()
        self._collection.delete(where={"date": {"$lt": date}})
        after = self._collection.count()
        return before - after

    def clean_by_stock(self, stock_code: str) -> int:
        if not self._available:
            return 0
        before = self._collection.count()
        self._collection.delete(where={"stock_code": stock_code})
        after = self._collection.count()
        return before - after

    def stats(self) -> dict:
        if not self._available:
            return {"backend": self.name(), "available": False}
        total = self._collection.count()
        result = {
            "backend": self.name(),
            "semantic_count": total,
            "episodic_count": 0,
            "db_size_mb": 0,
            "top_stocks": [],
            "top_sectors": [],
            "oldest_entry": None,
            "newest_entry": None,
        }
        if total == 0:
            return result
        try:
            all_data = self._collection.get(include=["metadatas"])
        except Exception:
            return result
        metas = all_data.get("metadatas", []) or []
        # top_stocks
        stock_counter: dict = {}
        for m in metas:
            code = m.get("stock_code", "")
            name = m.get("stock_name", "")
            if code:
                key = (code, name)
                stock_counter[key] = stock_counter.get(key, 0) + 1
        top_stocks = sorted(stock_counter.items(), key=lambda x: x[1], reverse=True)[:10]
        result["top_stocks"] = [
            {"code": k[0], "name": k[1], "count": v} for k, v in top_stocks
        ]
        # top_sectors
        sector_counter: dict = {}
        for m in metas:
            s = m.get("sector_l2", "")
            if s:
                sector_counter[s] = sector_counter.get(s, 0) + 1
        top_sectors = sorted(sector_counter.items(), key=lambda x: x[1], reverse=True)[:10]
        result["top_sectors"] = [
            {"sector": k, "count": v} for k, v in top_sectors
        ]
        # time range
        dates = [m.get("date", "") for m in metas if m.get("date")]
        if dates:
            result["oldest_entry"] = min(dates)
            result["newest_entry"] = max(dates)
        # db size
        chroma_path = get_chroma_data_path()
        if os.path.exists(chroma_path):
            total_size = 0
            for dirpath, _dirnames, filenames in os.walk(chroma_path):
                for f in filenames:
                    try:
                        total_size += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
            result["db_size_mb"] = round(total_size / (1024 * 1024), 2)
        return result

    def list_all(self, limit: int = 50, offset: int = 0) -> List[MemoryEntry]:
        if not self._available:
            return []
        results = self._collection.get(
            limit=limit, offset=offset,
            include=["metadatas", "documents"],
        )
        entries = []
        ids_list = results.get("ids", []) or []
        metas_list = results.get("metadatas", []) or []
        docs_list = results.get("documents", []) or []
        for i, entry_id in enumerate(ids_list):
            meta = metas_list[i] if i < len(metas_list) else {}
            doc = docs_list[i] if i < len(docs_list) else ""
            parsed_meta, conf, trace_run_id = self._parse_chroma_metadata(meta)
            lifecycle = parsed_meta.pop("_lifecycle")
            confidence_factors = parsed_meta.pop("_confidence_factors")
            provenance = parsed_meta.pop("_provenance")
            if trace_run_id and "trace_run_id" not in provenance:
                provenance["trace_run_id"] = trace_run_id
            entries.append(MemoryEntry(
                entry_id=entry_id,
                stock_code=meta.get("stock_code", ""),
                stock_name=meta.get("stock_name", ""),
                content=doc,
                metadata=parsed_meta,
                confidence=conf,
                confidence_factors=confidence_factors,
                provenance=provenance,
                **lifecycle,
            ))
        return entries

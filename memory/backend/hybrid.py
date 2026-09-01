"""
记忆系统 — Reranker（Cross-encoder 精排）+ Hybrid 后端（FTS5 + Embedding RRF 融合）+ 工厂函数
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import List, Optional, Callable

from utils.app_paths import get_stock_memory_db_path
from memory.backend.base import MemoryEntry, MemoryBackend
from memory.backend.fts5 import FTS5Backend
from memory.backend.embedding import EmbeddingBackend, _wait_embedding_ready, EMBEDDING_READY_TIMEOUT, EMBEDDING_READY_INTERVAL

_log = logging.getLogger(__name__)


# ------------------------------------------------------------
# Reranker — Cross-encoder 精排（可选）
# ------------------------------------------------------------
class Reranker:
    """Cross-encoder 精排器。

    支持三种模式:
      - "local": 本地 sentence-transformers CrossEncoder（推荐，零成本）
      - "openai": OpenAI-compatible /v1/rerank 端点
      - "":       不启用精排
    """

    def __init__(self, mode: str = "", model_name: str = "BAAI/bge-reranker-v2-m3",
                 api_key: str = "", api_base: str = "", local_dir: str = "",
                 idle_ttl: float = 120.0):
        self._mode = mode.strip()
        self._model_name = model_name
        self._api_key = api_key
        self._api_base = api_base
        self._local_dir = local_dir.strip() if local_dir else ""
        self._model = None
        self._load_attempted = False  # 懒加载：是否已尝试过加载本地模型
        self._load_lock = threading.Lock()  # 保护懒加载（后台预热线程 + 请求线程并发）
        # 空闲卸载：本地 CrossEncoder 加载后空闲超过 idle_ttl 秒自动从内存卸载。
        # idle_ttl<=0 表示「每次精排后立即卸载」（用户诉求：分析完即从内存排除）；
        # idle_ttl>0 表示活跃期间常驻、空闲后自动回收（平衡内存与延迟）。
        self._idle_ttl = max(0.0, float(idle_ttl))
        self._last_used = 0.0
        self._reaper_started = False
        self._reaper_stop = None
        self._reaper_wake = None
        # 关键：__init__ 不再加载 CrossEncoder（2GB+ 模型），否则会拖慢 web 启动。
        # 本地模型改为首次 rerank() 时懒加载；这里只根据配置判定「是否应可用」。
        if self._mode == "local":
            # 乐观判定：真正的加载与失败判定推迟到 _ensure_local_model()。
            self._available = True
        elif self._mode == "openai":
            self._available = bool(api_key and api_base)
        else:
            self._available = False

    def _ensure_local_model(self) -> bool:
        """首次使用时懒加载本地 CrossEncoder（线程安全）。返回是否加载成功。"""
        if self._model is not None:
            return True
        with self._load_lock:
            # double-check：可能已被后台预热线程加载好
            if self._model is not None:
                return True
            if self._load_attempted:
                return False  # 已尝试且失败，不再重试
            self._load_attempted = True
            try:
                from sentence_transformers import CrossEncoder
                if self._local_dir and os.path.isdir(self._local_dir):
                    # 精确本地路径：直接加载目录（路径可配置，零网络）
                    self._model = CrossEncoder(self._local_dir)
                    _log.info("[reranker] local cross-encoder lazily loaded from dir: %s", self._local_dir)
                else:
                    # 回退：从 HuggingFace 缓存加载（需 local_files_only=True 且已下载）
                    self._model = CrossEncoder(self._model_name, local_files_only=True)
                    _log.info("[reranker] local cross-encoder lazily loaded from cache: %s", self._model_name)
                return True
            except ImportError:
                _log.warning("[reranker] sentence-transformers 未安装，无法使用本地 reranker")
            except Exception:
                _log.warning("[reranker] 懒加载模型失败: %s", self._model_name, exc_info=True)
            self._available = False  # 加载失败，标记不可用，后续检索静默降级
            return False

    def warm(self) -> None:
        """后台预热：在独立 daemon 线程里懒加载模型，不阻塞调用方（如启动流程）。
        用于「启动不卡 + 首次检索也不慢」两全其美。"""
        if self._mode != "local" or self._model is not None or self._load_attempted:
            return
        threading.Thread(
            target=self._ensure_local_model, name="reranker-warm", daemon=True
        ).start()

    # ── 空闲卸载（内存回收）─────────────────────────────
    def _evict_unlocked(self) -> None:
        """在持有 _load_lock 的前提下卸载模型并尽量释放内存。"""
        self._model = None
        self._load_attempted = False  # 允许后续按需重新加载
        import gc
        gc.collect()
        # GPU 场景释放显存；CPU 场景 gc 已释放权重，empty_cache 无害
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        _log.info("[reranker] 模型已卸载，释放内存（idle/手动）")

    def evict(self) -> None:
        """手动/自动卸载本地模型，释放 ~2.3GB 常驻内存。"""
        if self._mode != "local":
            return
        with self._load_lock:
            if self._model is not None:
                self._evict_unlocked()
        # 通知回收线程退出
        if self._reaper_stop is not None:
            self._reaper_stop.set()

    def _start_reaper(self) -> None:
        """启动后台空闲回收线程（仅 local 模式、TTL>0 时需要常驻回收）。"""
        if self._mode != "local" or self._idle_ttl <= 0 or self._reaper_started:
            return
        self._reaper_started = True
        self._reaper_stop = threading.Event()
        self._reaper_wake = threading.Event()
        threading.Thread(target=self._reaper_loop, name="reranker-reaper", daemon=True).start()

    def _reaper_loop(self) -> None:
        """周期性检查：模型空闲超过 idle_ttl 即卸载。"""
        while not self._reaper_stop.is_set():
            # 等待下次轮询或被唤醒（TTL<=0 的即时卸载由 rerank 同步触发，不走此路径）
            self._reaper_wake.wait(15)
            self._reaper_wake.clear()
            if self._reaper_stop.is_set():
                return
            if self._model is None:
                continue
            idle = time.time() - self._last_used
            if idle >= self._idle_ttl:
                with self._load_lock:
                    if self._model is not None and (time.time() - self._last_used) >= self._idle_ttl:
                        self._evict_unlocked()

    def is_available(self) -> bool:
        return self._available

    def rerank(self, query: str, entries: List[MemoryEntry]) -> List[MemoryEntry]:
        """对候选条目精排，返回按精排分数降序的列表。"""
        if not self._available or not entries:
            return entries

        docs = [e.content for e in entries]

        if self._mode == "local":
            # 首次调用时懒加载模型（把 2GB+ 模型加载成本从启动期挪到第一次实际精排）
            if not self._ensure_local_model():
                return entries
            try:
                pairs = [[query, doc[:1024]] for doc in docs]
                scores = self._model.predict(pairs, show_progress_bar=False).tolist()
            except Exception:
                _log.warning("[reranker] local predict failed", exc_info=True)
                return entries
            # 记录使用时间并触发卸载策略
            self._last_used = time.time()
            if self._idle_ttl <= 0:
                # 用完即卸载（用户诉求：分析完成即从内存排除）；同步卸载安全
                # （predict 已返回，self._model 置 None 不影响在途调用持有的局部引用）
                with self._load_lock:
                    self._evict_unlocked()
            else:
                self._start_reaper()

        elif self._mode == "openai":
            # OpenAI SDK 无 client.rerank（会 AttributeError），改用原生 HTTP 调 /v1/rerank
            try:
                import httpx
                base = (self._api_base or "").rstrip("/")
                if not base.endswith("/v1"):
                    base += "/v1"
                resp = httpx.post(
                    f"{base}/rerank",
                    json={
                        "model": self._model_name,
                        "query": query,
                        "documents": [d[:1024] for d in docs],
                    },
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                score_map = {r["index"]: r["relevance_score"] for r in data.get("results", [])}
                scores = [score_map.get(i, 0.0) for i in range(len(docs))]
            except Exception:
                _log.warning("[reranker] openai rerank failed", exc_info=True)
                return entries
        else:
            return entries

        # 更新精排分（独立维度，绝不覆盖 e.score 原始检索分）
        for i, e in enumerate(entries):
            e.rerank_score = round(float(scores[i]), 4)
        entries.sort(key=lambda x: x.rerank_score, reverse=True)
        return entries


# ------------------------------------------------------------
# Hybrid 后端 — FTS5 + Embedding RRF 融合
# ------------------------------------------------------------
class HybridBackend(MemoryBackend):
    """FTS5 + Embedding 融合后端，RRF (Reciprocal Rank Fusion) 排序 + 可选 Cross-encoder 精排。

    写入同时写 FTS5 和 Embedding，检索时融合两路排名。
    Embedding 不可用时静默降级为纯 FTS5。
    """

    _RRF_K = 60  # RRF 常数

    def __init__(self, fts5: FTS5Backend, embedding: EmbeddingBackend,
                 reranker: Optional[Reranker] = None):
        self._fts5 = fts5
        self._embedding = embedding
        self._reranker = reranker

    def name(self) -> str:
        return "hybrid"

    def is_available(self) -> bool:
        return self._fts5.is_available()

    def get_all_raw(self, limit: int = 1000) -> List[dict]:
        """委托底层 FTS5 返回全部原始行（供修复）。"""
        return self._fts5.get_all_raw(limit=limit)

    # ── 写入 ──────────────────────────────────────────────

    def add(self, entry: MemoryEntry) -> str:
        eid = self._fts5.add(entry)
        if self._embedding.is_available():
            try:
                self._embedding.add(entry)
            except Exception:
                _log.warning("[hybrid] embedding add failed for eid=%s", eid, exc_info=True)
        return eid

    def add_batch(self, entries: List[MemoryEntry]) -> List[str]:
        ids = self._fts5.add_batch(entries)
        if self._embedding.is_available():
            try:
                self._embedding.add_batch(entries)
            except Exception:
                pass
        return ids

    # ── 检索：RRF 融合 ───────────────────────────────────

    def search(self, query: str, top_k: int = 5,
               confidence_threshold: float = 0.0,
               context: str = None) -> List[MemoryEntry]:
        """检索相关记忆。

        - 方案A（记忆拼接历史）：当传入 ``context``（session 历史 + 画像主题拼成的文本）
          时，对「当前问题」与「历史上下文」分别召回并做两路 RRF 融合，使与历史强相关、
          但当前提问未直接点名的记忆（如上一轮问过的大盘/CPO，本轮追问「补充数据」）也能召回。
        - 历史上下文路径权重(0.6)略低于当前问题(1.0)，避免稀释当前信号。
        """
        K = self._RRF_K
        emb_available = self._embedding.is_available()

        # ── 当前问题路径 ──
        fts5_cur = self._fts5.search(query, top_k=top_k * 3)
        emb_cur: list = []
        if emb_available:
            try:
                emb_cur = self._embedding.search(query, top_k=top_k * 3)
            except Exception:
                pass
            emb_cur = [entry for entry in emb_cur if self._fts5.is_active(entry.entry_id)]

        # ── 纯 FTS5 降级（无 embedding）：保持 BM25 量纲 + 置信度硬过滤 + 权重 + incomplete 排除 ──
        if not emb_available:
            results = list(fts5_cur)
            if context:
                ctx_hits = self._fts5.search(context, top_k=top_k * 3)
                seen = {e.entry_id for e in results}
                results.extend(e for e in ctx_hits if e.entry_id not in seen)
            if self._reranker and self._reranker.is_available():
                results = self._reranker.rerank(query, results)
            if confidence_threshold > 0:
                results = [e for e in results if e.score >= confidence_threshold]
            results = self._apply_confidence_weight(results)
            results = self._exclude_incomplete(results)
            _log.debug("[hybrid] search fts5-only hits=%d ctx=%s query=%r", len(results), bool(context), query[:60])
            return results[:top_k]

        # ── Hybrid：当前 + 历史上下文两路 RRF 融合 ──
        fts5_ctx: list = []
        emb_ctx: list = []
        if context:
            fts5_ctx = self._fts5.search(context, top_k=top_k * 3)
            try:
                emb_ctx = self._embedding.search(context, top_k=top_k * 3)
            except Exception:
                emb_ctx = []
            emb_ctx = [entry for entry in emb_ctx if self._fts5.is_active(entry.entry_id)]

        scores: dict = {}
        entry_map: dict = {}

        def _add(results, w):
            for rank, e in enumerate(results):
                scores[e.entry_id] = scores.get(e.entry_id, 0.0) + w / (K + rank + 1)
                entry_map.setdefault(e.entry_id, e)

        _add(fts5_cur, 1.0)
        _add(emb_cur, 1.0)
        if context:
            _add(fts5_ctx, 0.6)   # 历史上下文路径权重略低，避免稀释当前问题信号
            _add(emb_ctx, 0.6)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for entry_id, rrf_score in ranked[:top_k * 2]:
            entry = entry_map[entry_id]
            entry.score = round(rrf_score, 4)   # 检索相关性分（RRF 量纲）
            results.append(entry)

        # Cross-encoder 精排（按当前 query 重排；e.score 保持 RRF 原始分）
        if self._reranker and self._reranker.is_available():
            results = self._reranker.rerank(query, results)

        # v2 权重：置信度参与最终排序（🔴低信下沉 / 🟢高信上浮）
        results = self._apply_confidence_weight(results)

        # 排除「数据不足」失败残片（防自指循环）；空结果时回退保留
        results = self._exclude_incomplete(results)

        results = results[:top_k]

        _log.debug(
            "[hybrid] search hits=%d fts5=%d emb=%d ctx=%s rerank=%s query=%r",
            len(results), len(fts5_cur[:top_k]), len(emb_cur[:top_k]),
            bool(context),
            "yes" if (self._reranker and self._reranker.is_available()) else "no",
            query[:60],
        )
        return results

    @staticmethod
    def _apply_confidence_weight(results: List[MemoryEntry]) -> List[MemoryEntry]:
        """v2: 每条结果的最终分数 = 精排分 × 内容置信度 × 时间新鲜度（让低信/过期记忆下沉）。

        精排分优先取 rerank_score（Cross-encoder 0~1 量纲）；无 rerank 时回退 RRF/BM25 分。
        confidence 缺省按 0.5 处理，避免未打分记忆被清零。
        时间新鲜度：metadata.date 距今天越近权重越高（分档衰减，封底不归零），
        解决「今天/最近」类查询时语义相近的旧记忆反超新记忆的问题。
        """
        for e in results:
            base = e.rerank_score if getattr(e, "rerank_score", 0) else e.score
            conf = e.confidence if e.confidence > 0 else 0.5
            recency = HybridBackend._recency_factor(e)
            e.score = round(base * conf * recency, 4)
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    @staticmethod
    def _recency_factor(e: MemoryEntry) -> float:
        """时间新鲜度因子 [0.5, 1.0]：按 metadata.date 距今天数分档衰减。

        无日期/解析失败取 0.8（不惩罚，也不特别优待）；封底 0.5 保证
        历史规律类记忆（如长期逻辑）仍有竞争力，不会因时间被清零。
        """
        meta = getattr(e, "metadata", None) or {}
        date_str = meta.get("date") if isinstance(meta, dict) else None
        if not date_str:
            return 0.8
        try:
            d = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
            days = (datetime.now().astimezone() - d).days
        except (ValueError, TypeError):
            return 0.8
        if days < 0:
            return 1.0  # 未来时间戳（时钟偏差），视为最新
        if days <= 1:
            return 1.0   # 今天/昨天
        if days <= 3:
            return 0.95
        if days <= 7:
            return 0.9
        if days <= 30:
            return 0.8
        if days <= 90:
            return 0.65
        return 0.5       # 封底：3 个月以上

    @staticmethod
    def _is_incomplete(e: MemoryEntry) -> bool:
        """判断是否为「数据不足」失败残片。

        优先读归档时写入 provenance.incomplete（JSON 列会回环）；
        回退：内容内直接含「数据不足」标记。
        """
        prov = getattr(e, "provenance", None)
        if isinstance(prov, dict) and prov.get("incomplete"):
            return True
        return "数据不足" in (e.content or "")

    def _exclude_incomplete(self, results: List[MemoryEntry]) -> List[MemoryEntry]:
        """排除失败残片；若排除后为空，回退保留原结果（避免把正常记忆也误删）。"""
        kept = [e for e in results if not self._is_incomplete(e)]
        return kept if kept else results

    # ── 管理 ──────────────────────────────────────────────

    def delete(self, entry_id: str) -> bool:
        ok = self._fts5.delete(entry_id)
        if self._embedding.is_available():
            try:
                self._embedding.delete(entry_id)
            except Exception:
                pass
        return ok

    def mark_superseded(self, entry_ids: List[str], new_entry_id: str,
                        reason: str = "new_analysis") -> int:
        count = self._fts5.mark_superseded(entry_ids, new_entry_id, reason=reason)
        if self._embedding.is_available():
            try:
                self._embedding.mark_superseded(entry_ids, new_entry_id, reason=reason)
            except Exception:
                pass
        return count

    def clean_expired(self, as_of: str | None = None) -> int:
        count = self._fts5.clean_expired(as_of)
        if self._embedding.is_available():
            try:
                self._embedding.clean_expired(as_of)
            except Exception:
                pass
        return count

    def count(self) -> int:
        return self._fts5.count()

    def clean_before(self, date: str) -> int:
        fts5_count = self._fts5.clean_before(date)
        if self._embedding.is_available():
            try:
                self._embedding.clean_before(date)
            except Exception:
                pass
        return fts5_count

    def clean_by_stock(self, stock_code: str) -> int:
        fts5_count = self._fts5.clean_by_stock(stock_code)
        if self._embedding.is_available():
            try:
                self._embedding.clean_by_stock(stock_code)
            except Exception:
                pass
        return fts5_count

    def stats(self) -> dict:
        s = self._fts5.stats()
        s["backend"] = self.name()
        s["embedding_available"] = self._embedding.is_available()
        if self._embedding.is_available():
            emb_stats = self._embedding.stats()
            s["embedding_count"] = emb_stats.get("semantic_count", 0)
            s["embedding_size_mb"] = emb_stats.get("db_size_mb", 0)
        s["reranker_available"] = self._reranker.is_available() if self._reranker else False
        return s

    def list_all(self, limit: int = 50, offset: int = 0) -> List[MemoryEntry]:
        return self._fts5.list_all(limit=limit, offset=offset)

    def search_all(self, query: str, top_k: int = 20,
                   memory_type: str = "all") -> List[dict]:
        """管理查询：语义走 RRF（FTS5+Embedding），情景走 FTS5 LIKE"""
        results = []

        if memory_type in ("all", "semantic"):
            sem_entries = self.search(query, top_k=top_k)
            for e in sem_entries:
                results.append({
                    "type": "semantic",
                    "entry_id": e.entry_id,
                    "stock_code": e.stock_code,
                    "stock_name": e.stock_name,
                    "content": e.content[:200] if e.content else "",
                    "date": e.metadata.get("date", ""),
                    "score": e.score,
                    "tags": e.metadata.get("tags", []),
                })

        if memory_type in ("all", "episodic"):
            epi_results = self._fts5.search_all(query, top_k=top_k, memory_type="episodic")
            results.extend(epi_results)

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results[:top_k]

    def get_by_stock(self, stock_code: str, limit: int = 50) -> List[MemoryEntry]:
        """按股票代码列出历史记忆"""
        return self._fts5.get_by_stock(stock_code, limit=limit)


# ------------------------------------------------------------
# 工厂函数
# ------------------------------------------------------------
class _GatewayRerankAdapter:
    """把 LLM 网关的 rerank 适配成 Reranker 的 .rerank(query, entries) 接口。

    记忆系统的 reranker 因此也走统一网关（限流 / 重试 / 熔断），不再是旁路。
    网关不可用时由 ``_build_reranker`` 回退到原生 ``Reranker``。
    """

    def __init__(self):
        from utils.llm import gateway, Purpose
        self._gateway = gateway
        self._purpose = Purpose.MEMORY

    def is_available(self) -> bool:
        try:
            return self._gateway._get_rerank_provider().is_available()
        except Exception:
            return False

    def warm(self) -> None:
        try:
            self._gateway._get_rerank_provider().warm()
        except Exception:
            pass

    def evict(self) -> None:
        # 网关/API 路径不持有本地大模型，无内存可卸载
        return

    def rerank(self, query: str, entries: List[MemoryEntry]) -> List[MemoryEntry]:
        if not entries:
            return entries
        docs = [e.content for e in entries]
        scores = self._gateway.rerank(query, docs, purpose=self._purpose)
        # 与原生 Reranker 对齐：必须把精排分写回 e.rerank_score，
        # 否则 HybridBackend._apply_confidence_weight 的 base 回退到 RRF 分，
        # 会按 RRF 顺序重新排序，精排结果被整体打乱（白精排）。
        for e, s in zip(entries, scores):
            e.rerank_score = round(float(s), 4)
        scored = sorted(entries, key=lambda x: x.rerank_score, reverse=True)
        return scored


def _build_reranker() -> Optional[Reranker]:
    """从 Config 构建 Reranker。

    优先走统一 LLM 网关（_GatewayRerankAdapter）；网关不可用时回退到原生 Reranker。
    """
    try:
        from config import Config
        mode = Config.STOCK_MEMORY_RERANKER.strip()
        if not mode:
            return None
        # 优先网关路径
        try:
            return _GatewayRerankAdapter()
        except Exception:
            pass
        # 回退：原生 Reranker
        model = Config.STOCK_MEMORY_RERANKER_MODEL
        # Reranker 优先用自己的独立配置，未配置时回退到共享的 STOCK_MEMORY_*，再回退 OPENAI_*
        api_key = Config.STOCK_MEMORY_RERANKER_API_KEY or Config.STOCK_MEMORY_API_KEY or Config.OPENAI_API_KEY
        api_base = Config.STOCK_MEMORY_RERANKER_API_BASE or Config.STOCK_MEMORY_API_BASE or Config.OPENAI_API_BASE
        local_dir = getattr(Config, "STOCK_MEMORY_RERANKER_LOCAL_DIR", "")
        idle_ttl = getattr(Config, "STOCK_MEMORY_RERANKER_IDLE_TTL", 120.0)
        return Reranker(mode=mode, model_name=model, api_key=api_key,
                        api_base=api_base, local_dir=local_dir, idle_ttl=idle_ttl)
    except Exception:
        return None


def create_memory_backend(
    mode: str = "auto",
    embedding_fn: Optional[Callable] = None,
    db_path: Optional[str] = None,
    embedding_ready_timeout: float = EMBEDDING_READY_TIMEOUT,
    embedding_ready_interval: float = EMBEDDING_READY_INTERVAL,
    min_similarity: float = 0.0,
) -> MemoryBackend:
    """
    创建记忆检索后端。

    mode 取值:
      - "fts5":     仅 FTS5（零成本，默认）
      - "embedding": 仅 Embedding（需 embedding_fn，且服务必须就绪）
      - "hybrid":    FTS5 + Embedding 融合 + 可选 Rerank（需 embedding_fn）
      - "auto":      有 embedding_fn -> hybrid，否则 -> fts5
    """
    if db_path is None:
        db_path = get_stock_memory_db_path()

    fts5 = FTS5Backend(db_path)
    emb = EmbeddingBackend(embedding_fn=embedding_fn, min_similarity=min_similarity)
    reranker = _build_reranker()

    if mode == "fts5":
        return fts5

    if mode == "embedding":
        if not emb.is_available():
            raise ValueError("embedding 模式需要提供 embedding_fn 且 chromadb 已安装")
        if not _wait_embedding_ready(
            embedding_fn, embedding_ready_timeout, embedding_ready_interval
        ):
            raise RuntimeError(
                f"embedding 服务在 {embedding_ready_timeout:.0f}s 内未就绪，"
                f"无法满足 embedding 模式要求"
            )
        return emb

    if mode in ("hybrid", "auto"):
        # embedding 可用 → 完整 Hybrid（FTS5 + Embedding RRF + reranker）
        if emb.is_available():
            return HybridBackend(fts5, emb, reranker=reranker)
        # embedding 不可用（如 Ollama 未启动）时仍返回 HybridBackend：
        # 其内部 `not emb_available` 分支会用「FTS5 召回 + reranker 精排」，
        # 避免只配了 local reranker 却因 embedding 掉线导致精排被静默丢弃。
        if reranker is not None:
            return HybridBackend(fts5, emb, reranker=reranker)
        return fts5

    raise ValueError(f"未知 backend mode: {mode}，可选: auto / fts5 / embedding / hybrid")

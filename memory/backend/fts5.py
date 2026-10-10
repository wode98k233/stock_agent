"""
记忆系统 — FTS5 + jieba 全文检索后端（零成本，始终可用）
"""
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import List, Optional

from memory.backend.base import MemoryEntry, MemoryBackend
from utils.app_paths import get_jieba_cache_dir
from utils import sqlite_guard

_log = logging.getLogger(__name__)


# ── jieba 一次性初始化 ──────────────────────────────────────
# jieba 首次分词时会 build 前缀词典（~0.4s）并把 'Building prefix dict...'
# 之类的 INFO 打到 stdout（绕开项目日志器），且默认把缓存写到系统 temp
# （C:\Users\...\Temp）。这里在模块导入期（启动期）完成初始化：静音日志 +
# 把缓存重定向到项目 cache 目录，避免在请求链路里卡顿并污染日志。
_JIEBA = None


_JIEBA_DISABLED = False  # 由 ensure_jieba_ready 根据 Config.JIEBA_ENABLED 设置


def ensure_jieba_ready(logger=None):
    """幂等地初始化 jieba。当 JIEBA_ENABLED=false 时跳过，分词退化为原文匹配。

    禁用 jieba 可节省 ~40MB RSS（词典内存），代价是中文分词精度下降。
    对英文/数字查询无影响。
    """
    global _JIEBA, _JIEBA_DISABLED
    if _JIEBA is not None or _JIEBA_DISABLED:
        return _JIEBA
    # 检查是否启用 jieba
    try:
        from config import Config
        if not Config.JIEBA_ENABLED:
            _JIEBA_DISABLED = True
            if logger:
                logger.info("[jieba] 已禁用（JIEBA_ENABLED=false），FTS5 分词降级为原文匹配")
            return None
    except Exception:
        pass  # config 不可用时仍然尝试加载 jieba
    try:
        import jieba
        try:
            jieba.setLogLevel(logging.ERROR)
        except Exception:
            pass
        try:
            cache_dir = get_jieba_cache_dir()
            os.makedirs(cache_dir, exist_ok=True)
            jieba.dt.tmp_dir = cache_dir
        except Exception:
            pass
        jieba.initialize()
        _JIEBA = jieba
        if logger:
            logger.debug("[jieba] 初始化完成，缓存目录=%s",
                         getattr(jieba.dt, "tmp_dir", "?"))
    except Exception as e:
        _JIEBA = None
        if logger:
            logger.warning("[jieba] 初始化失败，FST5 分词退化为原文: %s", e)
    return _JIEBA


# 模块导入即初始化：memory.backend 在启动期被导入，保证词典 build 发生在启动而非请求期
# 注意：bootstrap 层（cli/bootstrap.py、server/bootstrap.py）会在 Memory SDK 初始化时
# 显式调用 ensure_jieba_ready()。此处不再自动触发，避免在 import 链路上阻塞 0.5s+。
# _segment() 在 jieba 未就绪时自动降级为原文匹配。


# ------------------------------------------------------------
# FTS5 + jieba 后端（零成本，始终可用）
# ------------------------------------------------------------
class FTS5Backend(MemoryBackend):
    """基于 SQLite FTS5 + jieba 中文分词的全文检索后端"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        # ── 每线程一个连接（2026-09-21）────────────────────────────
        # 本后端是单例，但会被「事件循环线程 / asyncio worker 线程 / 记忆预热
        # 线程 / 定时清理协程」同时使用。原先共用一个
        # `sqlite3.connect(..., check_same_thread=False)`，`check_same_thread=False`
        # 只关掉了 Python 层校验、**并不会让连接变线程安全**：并发 execute/reset
        # 会互相踩（实测产出 `another row available` / `cannot commit - no
        # transaction is active` 这类脏错误），也是同类 native 崩溃的风险面。
        # 改为一连接一线程（与 server/monitoring/store.py、signal_eval/db.py
        # 的既有写法一致），从构造上消除共享。
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._conns: List[sqlite3.Connection] = []
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._init_db()

    def name(self) -> str:
        return "fts5"

    def is_available(self) -> bool:
        return True

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite_guard.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with self._conns_lock:
                self._conns.append(conn)
            self._local.conn = conn
        if not self._schema_ready:
            with self._schema_lock:
                if not self._schema_ready:
                    self._init_db(conn)
                    self._schema_ready = True
        return conn

    def close(self) -> None:
        """关闭本后端创建过的全部连接（幂等；进程收尾用）。"""
        with self._conns_lock:
            conns, self._conns = list(self._conns), []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()
        self._schema_ready = False

    def _init_db(self, conn: Optional[sqlite3.Connection] = None):
        """建表/迁移走统一 schema（stock_memory 组，幂等加性）。

        FTS5 虚拟表、元数据/情景/冲突日志表、v2 列迁移与依赖索引的单一事实源在
        utils/db_schema/tables/memory.py。
        """
        from utils.db_schema.migrator import ensure_schema
        from utils.db_schema.registry import ALL_DBS
        d = ALL_DBS["stock_memory"]
        ensure_schema(conn or self._get_conn(), d.tables, d.special_migrations)

    @staticmethod
    def _segment(text: str) -> str:
        """jieba 中文分词，空格连接（jieba 已在模块导入时初始化）"""
        if _JIEBA is None:
            return text
        return " ".join(_JIEBA.cut(text))

    # FTS5 查询停用词：疑问/语气/虚词 + 高频无信息量词。
    # 这些词若参与 OR 检索会大量命中无关记忆，过滤后只保留有区分度的业务词。
    _QUERY_STOPWORDS = frozenset(
        """
        的 了 吗 呢 啊 吧 呀 么 嘛 哦 咋 怎么 怎样 如何 什么 为啥 为什么
        哪些 哪个 谁 多少 几 多 最近 今天 昨天 明天 现在 目前 当前 已经
        是否 有没有 会不会 能 可以 应该 要 想 知道 请问 帮 麻烦
        我 你 他 她 它 我们 你们 他们 这个 那个 一个 一下 怎么样 啥
        之 与 及 并 或 就 都 也 很 太 挺 更 最 点 只 仅 大概 大约 左右
        起来 出来 过来 下去 觉得 感觉 看看 讲讲 说说 聊聊 一下
        """.split()
    )
    _QUERY_MAX_TOKENS = 6  # OR 检索最多取前 N 个（按长度降序），避免泛词稀释

    @staticmethod
    def _fts5_query(text: str) -> str:
        """将 jieba 分词结果转为 FTS5 OR 查询。

        FTS5 空格分隔是隐式 AND：分词出的多个词必须**全部**命中才返回。
        对用户完整问题（如「宁德时代最近走势如何」→ 5 个词）几乎必然 0 召回，
        故这里用 OR 语义——任一业务词命中即召回，精度交给 bm25 排序 +
        （Hybrid 模式下）Embedding RRF 融合 + Cross-encoder 精排。
        token 按长度降序截断到 _QUERY_MAX_TOKENS 个，双引号包裹避免 FTS5 语法歧义。
        """
        tokens = text.split()
        if not tokens:
            return text
        kept = [t for t in tokens if t not in FTS5Backend._QUERY_STOPWORDS]
        if not kept:
            return text  # 全是停用词时回退原文（单 token，无 AND 问题）
        # 按长度降序取前 N 个：长词（股票名/板块名/业务词）更有区分度
        kept.sort(key=len, reverse=True)
        kept = kept[:FTS5Backend._QUERY_MAX_TOKENS]
        return " OR ".join(f'"{t}"' for t in kept)

    # ── 写入 ──────────────────────────────────────────────

    def add(self, entry: MemoryEntry) -> str:
        if not entry.entry_id:
            entry.entry_id = f"{entry.stock_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        conn = self._get_conn()
        content_seg = self._segment(entry.content)
        # 股票名也分词，确保"茅台"等简称能命中
        name_seg = self._segment(entry.stock_name) if entry.stock_name else ""
        meta = entry.metadata or {}

        # 先删后插：FTS5 虚拟表无 UNIQUE 约束，INSERT OR REPLACE 会追加重复行，
        # 必须显式按 entry_id 删除旧记录，保证每个 entry_id 仅一条 FTS5 记录。
        conn.execute(
            "DELETE FROM semantic_memory_fts WHERE entry_id = ?", (entry.entry_id,)
        )
        conn.execute(
            "INSERT INTO semantic_memory_fts "
            "(entry_id, stock_code, stock_name, content_seg, raw_content, date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry.entry_id, entry.stock_code, name_seg,
             content_seg, entry.content,
             meta.get("date", datetime.now().isoformat()))
        )
        conn.execute(
            "INSERT OR REPLACE INTO semantic_memory_meta "
            "(entry_id, stock_code, stock_name, sector_l1, sector_l2, "
            "pe, roe, volatility, tags, sentiment, source_query, date, "
            "chunk_index, chunk_count, chunk_group_id, "
            "confidence, confidence_factors, provenance, trace_run_id, "
            "memory_category, ttl_days, expires_at, last_validated_at, "
            "superseded_by, superseded_at, supersede_reason, subject_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.entry_id, entry.stock_code, entry.stock_name,
             meta.get("sector_l1"), meta.get("sector_l2"),
             meta.get("pe"), meta.get("roe"),
             meta.get("volatility"), json.dumps(meta.get("tags", []), ensure_ascii=False),
             meta.get("sentiment"), meta.get("source_query"),
             meta.get("date", datetime.now().isoformat()),
             int(meta.get("chunk_index") or 0),
             int(meta.get("chunk_count") or 1),
             meta.get("chunk_group_id") or None,
             entry.confidence,
             json.dumps(entry.confidence_factors, ensure_ascii=False) if entry.confidence_factors else None,
             json.dumps(entry.provenance, ensure_ascii=False) if entry.provenance else None,
             entry.provenance.get("trace_run_id", "") if entry.provenance else "",
             entry.memory_category,
             entry.ttl_days,
             entry.expires_at or None,
             entry.last_validated_at or None,
             entry.superseded_by or None,
             entry.superseded_at or None,
             entry.supersede_reason or None,
             meta.get("subject_kind") or None)
        )
        conn.commit()
        return entry.entry_id

    # ── 检索 ──────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> List[MemoryEntry]:
        conn = self._get_conn()
        query_seg = self._segment(query)
        fts5_q = self._fts5_query(query_seg)  # OR 语义，避免 AND 导致零召回
        try:
            rows = conn.execute(
                "SELECT f.entry_id, f.stock_code, f.stock_name, f.raw_content, f.date, "
                "bm25(semantic_memory_fts) AS score, "
                "m.sector_l1, m.sector_l2, m.pe, m.roe, m.volatility, m.tags, m.sentiment, "
                "m.source_query, m.confidence, m.confidence_factors, m.provenance, "
                "m.memory_category, m.ttl_days, m.expires_at, m.last_validated_at, "
                "m.superseded_by, m.subject_kind, m.superseded_at, m.supersede_reason, "
                "m.chunk_index, m.chunk_count, m.chunk_group_id "
                "FROM semantic_memory_fts f "
                "LEFT JOIN semantic_memory_meta m ON f.entry_id = m.entry_id "
                "WHERE semantic_memory_fts MATCH ? "
                "AND (m.superseded_by IS NULL OR m.superseded_by = '') "
                "AND (m.expires_at IS NULL OR m.expires_at = '' OR m.expires_at > ?) "
                "ORDER BY score LIMIT ?",
                (fts5_q, datetime.now().isoformat(), top_k)
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        results = []
        for r in rows:
            meta = {
                "date": r[4] or "",
                "sector_l1": r[6], "sector_l2": r[7],
                "pe": r[8], "roe": r[9],
                "volatility": r[10], "sentiment": r[12],
                "source_query": r[13] or "",
                "subject_kind": r[22] if len(r) > 22 and r[22] else "",
                "chunk_index": int(r[25] or 0) if len(r) > 25 else 0,
                "chunk_count": int(r[26] or 1) if len(r) > 26 else 1,
                "chunk_group_id": (r[27] if len(r) > 27 else "") or "",
            }
            try:
                meta["tags"] = json.loads(r[11]) if r[11] else []
            except json.JSONDecodeError:
                meta["tags"] = []
            # v2 confidence / provenance 反序列化
            confidence = r[14] if r[14] is not None else 0.5
            confidence_factors = {}
            try:
                confidence_factors = json.loads(r[15]) if r[15] else {}
            except json.JSONDecodeError:
                pass
            provenance = {}
            try:
                provenance = json.loads(r[16]) if r[16] else {}
            except json.JSONDecodeError:
                pass
            results.append(MemoryEntry(
                entry_id=r[0], stock_code=r[1], stock_name=r[2],
                content=r[3], score=abs(r[5]) if r[5] else 0,
                metadata=meta,
                confidence=confidence,
                confidence_factors=confidence_factors,
                provenance=provenance,
                memory_category=r[17] if len(r) > 17 and r[17] else "general",
                ttl_days=r[18] if len(r) > 18 and r[18] is not None else 30,
                expires_at=r[19] if len(r) > 19 and r[19] else "",
                last_validated_at=r[20] if len(r) > 20 and r[20] else "",
                superseded_by=r[21] if len(r) > 21 and r[21] else "",
                superseded_at=r[23] if len(r) > 23 and r[23] else "",
                supersede_reason=r[24] if len(r) > 24 and r[24] else "",
            ))
        return results

    def search_structured(self, conditions: dict, top_k: int = 10) -> List[MemoryEntry]:
        """结构化查询：按标签/行业/PE 范围等精确检索"""
        conn = self._get_conn()
        where = []
        params = []

        if conditions.get("stock_code"):
            where.append("stock_code = ?")
            params.append(conditions["stock_code"])
        if conditions.get("stock_name"):
            where.append("stock_name LIKE ?")
            params.append(f"%{conditions['stock_name']}%")
        if conditions.get("sector_l2"):
            where.append("sector_l2 = ?")
            params.append(conditions["sector_l2"])
        if conditions.get("pe_min") is not None:
            where.append("pe >= ?")
            params.append(conditions["pe_min"])
        if conditions.get("pe_max") is not None:
            where.append("pe <= ?")
            params.append(conditions["pe_max"])
        if conditions.get("volatility"):
            where.append("volatility = ?")
            params.append(conditions["volatility"])
        if conditions.get("sentiment"):
            where.append("sentiment = ?")
            params.append(conditions["sentiment"])

        sql = "SELECT * FROM semantic_memory_meta"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(top_k)

        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            meta = {
                "sector_l1": r[4], "sector_l2": r[5],
                "pe": r[6], "roe": r[7],
                "volatility": r[8], "sentiment": r[10],
            }
            try:
                meta["tags"] = json.loads(r[9]) if r[9] else []
            except json.JSONDecodeError:
                meta["tags"] = []
            results.append(MemoryEntry(
                entry_id=r[0], stock_code=r[1], stock_name=r[2],
                content="", score=0, metadata=meta,
            ))
        return results

    # ── v2: 多源印证查询 ─────────────────────────────────

    def get_recent_same_conclusions(self, stock_code: str, sentiment: str,
                                     days: int = 7) -> list:
        """查询 N 天内同 stock_code + 同 sentiment 的结论列表。

        供 ConfidenceCalculator.compute_corroboration() 使用。
        """
        if not stock_code or not sentiment:
            return []
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT stock_code, sentiment, date FROM semantic_memory_meta "
            "WHERE stock_code = ? AND sentiment = ? "
            "AND date >= date('now', ?) "
            "ORDER BY date DESC LIMIT 10",
            (stock_code, sentiment, f"-{days} days")
        ).fetchall()
        return [
            {"stock_code": r[0], "sentiment": r[1], "date": r[2]}
            for r in rows
        ]

    # ── 管理 ──────────────────────────────────────────────

    def delete(self, entry_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT created_at FROM semantic_memory_meta WHERE entry_id = ?", (entry_id,)
        ).fetchone()

        conn.execute("DELETE FROM semantic_memory_fts WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM semantic_memory_meta WHERE entry_id = ?", (entry_id,))

        if row and row[0]:
            created_at = row[0]
            conn.execute(
                "DELETE FROM episode_log WHERE created_at BETWEEN datetime(?, '-2 seconds') AND datetime(?, '+2 seconds')",
                (created_at, created_at)
            )

        conn.commit()
        return True

    def mark_superseded(self, entry_ids: List[str], new_entry_id: str,
                        reason: str = "new_analysis") -> int:
        ids = [entry_id for entry_id in entry_ids if entry_id and entry_id != new_entry_id]
        if not ids:
            return 0
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in ids)
        params = [new_entry_id, datetime.now().isoformat(), reason, *ids]
        cursor = conn.execute(
            f"UPDATE semantic_memory_meta "
            f"SET superseded_by = ?, superseded_at = ?, supersede_reason = ? "
            f"WHERE entry_id IN ({placeholders}) AND entry_id != ?",
            [*params, new_entry_id],
        )
        conn.commit()
        return cursor.rowcount

    def is_active(self, entry_id: str, as_of: str | None = None) -> bool:
        as_of = as_of or datetime.now().isoformat()
        row = self._get_conn().execute(
            "SELECT 1 FROM semantic_memory_meta "
            "WHERE entry_id = ? "
            "AND (superseded_by IS NULL OR superseded_by = '') "
            "AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)",
            (entry_id, as_of),
        ).fetchone()
        return row is not None

    def clean_expired(self, as_of: str | None = None) -> int:
        as_of = as_of or datetime.now().isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT entry_id FROM semantic_memory_meta "
            "WHERE expires_at IS NOT NULL AND expires_at != '' AND expires_at <= ?",
            (as_of,),
        ).fetchall()
        ids = [row[0] for row in rows]
        if not ids:
            return 0
        conn.executemany(
            "DELETE FROM semantic_memory_fts WHERE entry_id = ?",
            [(entry_id,) for entry_id in ids],
        )
        conn.executemany(
            "DELETE FROM semantic_memory_meta WHERE entry_id = ?",
            [(entry_id,) for entry_id in ids],
        )
        conn.commit()
        return len(ids)

    def count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM semantic_memory_fts").fetchone()
        return row[0] if row else 0

    def clean_before(self, date: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory_fts WHERE date < ?", (date,)
        ).fetchone()
        sem_count = row[0] if row else 0
        epi_row = conn.execute(
            "SELECT COUNT(*) FROM episode_log WHERE date < ?", (date,)
        ).fetchone()
        epi_count = epi_row[0] if epi_row else 0

        if sem_count == 0 and epi_count == 0:
            return 0

        conn.execute("DELETE FROM semantic_memory_fts WHERE date < ?", (date,))
        conn.execute("DELETE FROM semantic_memory_meta WHERE date < ?", (date,))
        conn.execute("DELETE FROM episode_log WHERE date < ?", (date,))
        conn.commit()
        conn.execute("INSERT INTO semantic_memory_fts(semantic_memory_fts) VALUES('optimize')")
        conn.commit()
        return sem_count + epi_count

    def clean_by_stock(self, stock_code: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory_fts WHERE stock_code = ?", (stock_code,)
        ).fetchone()
        sem_count = row[0] if row else 0
        epi_row = conn.execute(
            "SELECT COUNT(*) FROM episode_log WHERE stocks_mentioned LIKE ?",
            (f"%{stock_code}%",)
        ).fetchone()
        epi_count = epi_row[0] if epi_row else 0

        if sem_count == 0 and epi_count == 0:
            return 0

        conn.execute("DELETE FROM semantic_memory_fts WHERE stock_code = ?", (stock_code,))
        conn.execute("DELETE FROM semantic_memory_meta WHERE stock_code = ?", (stock_code,))
        conn.execute("DELETE FROM episode_log WHERE stocks_mentioned LIKE ?", (f"%{stock_code}%",))
        conn.commit()
        return sem_count + epi_count

    def stats(self) -> dict:
        conn = self._get_conn()
        total = self.count()
        db_size = os.path.getsize(self._db_path) if os.path.exists(self._db_path) else 0
        top_stocks = conn.execute(
            "SELECT stock_code, stock_name, COUNT(*) AS cnt FROM semantic_memory_meta "
            "GROUP BY stock_code ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        top_sectors = conn.execute(
            "SELECT sector_l2, COUNT(*) AS cnt FROM semantic_memory_meta "
            "WHERE sector_l2 IS NOT NULL AND sector_l2 != '' "
            "GROUP BY sector_l2 ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        oldest = conn.execute("SELECT MIN(date) FROM semantic_memory_fts").fetchone()
        newest = conn.execute("SELECT MAX(date) FROM semantic_memory_fts").fetchone()
        epi_count = conn.execute("SELECT COUNT(*) FROM episode_log").fetchone()

        return {
            "backend": self.name(),
            "semantic_count": total,
            "episodic_count": epi_count[0] if epi_count else 0,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "top_stocks": [{"code": r[0], "name": r[1], "count": r[2]} for r in top_stocks],
            "top_sectors": [{"sector": r[0], "count": r[1]} for r in top_sectors],
            "oldest_entry": oldest[0] if oldest and oldest[0] else None,
            "newest_entry": newest[0] if newest and newest[0] else None,
        }

    def list_all(self, limit: int = 50, offset: int = 0) -> List[MemoryEntry]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT f.entry_id, f.stock_code, f.stock_name, f.raw_content, f.date, "
            "m.sector_l1, m.sector_l2, m.pe, m.roe, m.tags, m.sentiment, m.source_query, "
            "m.confidence, m.confidence_factors, m.provenance, "
            "m.chunk_index, m.chunk_count, m.chunk_group_id "
            "FROM semantic_memory_fts f "
            "LEFT JOIN semantic_memory_meta m ON f.entry_id = m.entry_id "
            "ORDER BY f.date DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        results = []
        for r in rows:
            meta = {
                "date": r[4] or "",
                "sector_l1": r[5], "sector_l2": r[6], "pe": r[7], "roe": r[8],
                "sentiment": r[10], "source_query": r[11] or "",
                "chunk_index": int(r[15] or 0) if len(r) > 15 else 0,
                "chunk_count": int(r[16] or 1) if len(r) > 16 else 1,
                "chunk_group_id": (r[17] if len(r) > 17 else "") or "",
            }
            try:
                meta["tags"] = json.loads(r[9]) if r[9] else []
            except json.JSONDecodeError:
                meta["tags"] = []
            confidence = r[12] if len(r) > 12 and r[12] is not None else 0.5
            confidence_factors = {}
            try:
                confidence_factors = json.loads(r[13]) if len(r) > 13 and r[13] else {}
            except json.JSONDecodeError:
                pass
            provenance = {}
            try:
                provenance = json.loads(r[14]) if len(r) > 14 and r[14] else {}
            except json.JSONDecodeError:
                pass
            results.append(MemoryEntry(
                entry_id=r[0], stock_code=r[1], stock_name=r[2],
                content=r[3], metadata=meta,
                confidence=confidence,
                confidence_factors=confidence_factors,
                provenance=provenance,
            ))
        return results

    def get_all_raw(self, limit: int = 1000) -> List[dict]:
        """返回全部语义记忆原始行（以 meta 为准，含 source_query 与 raw_content），供修复使用。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT m.entry_id, m.stock_code, m.stock_name, f.raw_content, "
            "m.sector_l1, m.sector_l2, m.pe, m.roe, m.volatility, "
            "m.tags, m.sentiment, m.source_query, m.date, "
            "m.confidence, m.confidence_factors, m.provenance, m.trace_run_id, "
            "m.chunk_index, m.chunk_count, m.chunk_group_id, m.rowid "
            "FROM semantic_memory_meta m "
            "LEFT JOIN semantic_memory_fts f ON f.entry_id = m.entry_id "
            "ORDER BY m.date DESC LIMIT ?",
            (limit,)
        ).fetchall()
        out = []
        for r in rows:
            try:
                tags = json.loads(r[9]) if r[9] else []
            except json.JSONDecodeError:
                tags = []
            confidence_factors = {}
            try:
                confidence_factors = json.loads(r[14]) if r[14] else {}
            except json.JSONDecodeError:
                pass
            provenance = {}
            try:
                provenance = json.loads(r[15]) if r[15] else {}
            except json.JSONDecodeError:
                pass
            out.append({
                "entry_id": r[0], "stock_code": r[1], "stock_name": r[2],
                "raw_content": r[3], "sector_l1": r[4], "sector_l2": r[5],
                "pe": r[6], "roe": r[7], "volatility": r[8],
                "tags": tags, "sentiment": r[10],
                "source_query": r[11], "date": r[12],
                "confidence": r[13] if r[13] is not None else 0.5,
                "confidence_factors": confidence_factors,
                "provenance": provenance,
                "trace_run_id": r[16] or "",
                "chunk_index": int(r[17] or 0) if len(r) > 17 else 0,
                "chunk_count": int(r[18] or 1) if len(r) > 18 else 1,
                "chunk_group_id": (r[19] if len(r) > 19 else "") or "",
                "rowid": r[20] if len(r) > 20 else 0,
            })
        return out

    def backfill_chunk_columns(self, records) -> int:
        """回填 chunk 元数据列（存量数据修复）。

        records: 可迭代 ``(chunk_index, chunk_count, chunk_group_id, entry_id)``。
        幂等由调用方按需控制（默认不带 where，直接覆盖）。
        """
        conn = self._get_conn()
        n = 0
        for chunk_index, chunk_count, chunk_group_id, entry_id in records:
            conn.execute(
                "UPDATE semantic_memory_meta SET chunk_index=?, chunk_count=?, chunk_group_id=? "
                "WHERE entry_id=?",
                (int(chunk_index), int(chunk_count), chunk_group_id, entry_id),
            )
            n += 1
        conn.commit()
        return n

    def get_by_stock(self, stock_code: str, limit: int = 50) -> List[MemoryEntry]:
        """按股票代码列出历史记忆"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT f.entry_id, f.stock_code, f.stock_name, f.raw_content, f.date, "
            "m.source_query, m.confidence, m.confidence_factors, m.provenance, "
            "m.memory_category, m.ttl_days, m.expires_at, m.last_validated_at, "
            "m.superseded_by, m.superseded_at, m.supersede_reason, m.subject_kind, "
            "m.chunk_index, m.chunk_count, m.chunk_group_id "
            "FROM semantic_memory_fts f "
            "LEFT JOIN semantic_memory_meta m ON f.entry_id = m.entry_id "
            "WHERE f.stock_code = ? "
            "ORDER BY f.date DESC LIMIT ?",
            (stock_code, limit)
        ).fetchall()
        results = []
        for r in rows:
            try:
                confidence_factors = json.loads(r[7]) if r[7] else {}
            except json.JSONDecodeError:
                confidence_factors = {}
            try:
                provenance = json.loads(r[8]) if r[8] else {}
            except json.JSONDecodeError:
                provenance = {}
            results.append(MemoryEntry(
                entry_id=r[0], stock_code=r[1], stock_name=r[2], content=r[3],
                metadata={
                    "date": r[4] or "",
                    "source_query": r[5] or "",
                    "subject_kind": r[16] or "",
                    "chunk_index": int(r[17] or 0) if len(r) > 17 else 0,
                    "chunk_count": int(r[18] or 1) if len(r) > 18 else 1,
                    "chunk_group_id": (r[19] if len(r) > 19 else "") or "",
                },
                confidence=r[6] if r[6] is not None else 0.5,
                confidence_factors=confidence_factors,
                provenance=provenance,
                memory_category=r[9] or "general",
                ttl_days=r[10] if r[10] is not None else 30,
                expires_at=r[11] or "",
                last_validated_at=r[12] or "",
                superseded_by=r[13] or "",
                superseded_at=r[14] or "",
                supersede_reason=r[15] or "",
            ))
        return results

    def search_all(self, query: str, top_k: int = 20,
                   memory_type: str = "all") -> List[dict]:
        """管理查询：按关键词搜索语义+情景记忆"""
        conn = self._get_conn()
        results = []

        if memory_type in ("all", "semantic"):
            sem_results = self.search(query, top_k=top_k)
            for e in sem_results:
                results.append({
                    "type": "semantic",
                    "entry_id": e.entry_id,
                    "stock_code": e.stock_code,
                    "stock_name": e.stock_name,
                    "content": e.content[:200] if e.content else "",
                    "date": e.metadata.get("date", ""),
                    "score": e.score,
                    "tags": e.metadata.get("tags", []),
                    "confidence": e.confidence,
                    "provenance": e.provenance,
                })

        if memory_type in ("all", "episodic"):
            query_seg = self._segment(query)
            try:
                epi_rows = conn.execute(
                    "SELECT id, date, query, topics, key_findings, conclusion, "
                    "stocks_mentioned, tags FROM episode_log "
                    "WHERE query LIKE ? OR conclusion LIKE ? OR key_findings LIKE ? "
                    "ORDER BY date DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", f"%{query}%", top_k)
                ).fetchall()
            except sqlite3.OperationalError:
                epi_rows = []
            for r in epi_rows:
                try:
                    topics = json.loads(r[3]) if r[3] else []
                except json.JSONDecodeError:
                    topics = []
                results.append({
                    "type": "episodic",
                    "entry_id": r[0],
                    "stock_code": "",
                    "stock_name": "",
                    "content": r[1][:200],
                    "date": r[1],
                    "score": 0,
                    "tags": topics,
                })

        results.sort(key=lambda x: x.get("date", ""), reverse=True)
        return results[:top_k]

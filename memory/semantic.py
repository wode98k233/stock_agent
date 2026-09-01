"""
记忆系统 — 语义记忆封装
在 MemoryBackend 之上提供股票领域语义（按股票/板块/标签检索、相似股票推荐等）
"""
from typing import List, Optional, Dict
from datetime import datetime

from memory.backend import MemoryBackend, MemoryEntry


class SemanticMemory:
    """语义记忆——存储和检索股票分析结论"""

    def __init__(self, backend: MemoryBackend):
        self._backend = backend

    # ── 写入 ──────────────────────────────────────────────

    def add(self, entry: MemoryEntry) -> str:
        """写入一条分析记忆"""
        if not entry.metadata.get("date"):
            entry.metadata["date"] = datetime.now().isoformat()
        return self._backend.add(entry)

    def add_analysis(self, stock_code: str, stock_name: str,
                     content: str, metadata: Optional[Dict] = None) -> str:
        """快捷写入：从分析结果直接构建 MemoryEntry"""
        entry = MemoryEntry(
            entry_id="",
            stock_code=stock_code,
            stock_name=stock_name,
            content=content,
            metadata=metadata or {},
        )
        return self.add(entry)

    # ── 检索 ──────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """全文检索（默认使用 FTS5 BM25）"""
        return self._backend.search(query, top_k=top_k)

    def search_structured(self, conditions: dict, top_k: int = 10) -> List[MemoryEntry]:
        """结构化检索：按行业/PE 范围/波动性等精确查询"""
        if hasattr(self._backend, "search_structured"):
            return self._backend.search_structured(conditions, top_k=top_k)
        return []

    def find_similar(self, stock_code: str, top_k: int = 5) -> List[MemoryEntry]:
        """基于结构化标签找相似股票（规则匹配，非向量）"""
        # 先查目标股票的标签
        existing = self._backend.search_structured(
            {"stock_code": stock_code}, top_k=1
        ) if hasattr(self._backend, "search_structured") else []

        if not existing:
            return []

        target = existing[0]
        conditions = {}
        if target.metadata.get("sector_l2"):
            conditions["sector_l2"] = target.metadata["sector_l2"]
        if target.metadata.get("pe") is not None:
            pe = target.metadata["pe"]
            conditions["pe_min"] = pe * 0.5
            conditions["pe_max"] = pe * 1.5

        results = self._backend.search_structured(conditions, top_k=top_k + 1) \
            if hasattr(self._backend, "search_structured") else []
        # 排除自己
        return [r for r in results if r.stock_code != stock_code][:top_k]

    def get_by_stock(self, stock_code: str, limit: int = 50) -> List[MemoryEntry]:
        """按股票代码列出历史分析"""
        if hasattr(self._backend, "get_by_stock"):
            return self._backend.get_by_stock(stock_code, limit=limit)
        return []

    def get_recent(self, days: int = 7, limit: int = 30) -> List[MemoryEntry]:
        """最近 N 天的分析记录"""
        return self._backend.list_all(limit=limit)

    # ── 管理 ──────────────────────────────────────────────

    def stats(self) -> dict:
        """统计信息"""
        return self._backend.stats()

    def delete(self, entry_id: str) -> bool:
        return self._backend.delete(entry_id)

    def clean_before(self, date: str) -> int:
        return self._backend.clean_before(date)

    def clean_by_stock(self, stock_code: str) -> int:
        return self._backend.clean_by_stock(stock_code)

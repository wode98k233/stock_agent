"""
记忆系统 — 数据模型 + 检索后端抽象基类
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List

_log = logging.getLogger(__name__)


# ------------------------------------------------------------
# 数据模型
# ------------------------------------------------------------
@dataclass
class MemoryEntry:
    """记忆条目——所有后端统一使用"""
    entry_id: str
    stock_code: str
    stock_name: str
    content: str                        # 原始分析文本
    score: float = 0.0                  # 检索得分（原始检索分：FTS5=BM25，hybrid=RRF，embedding=余弦相似度）
    rerank_score: float = 0.0          # Cross-encoder 精排分（仅用于重排，与 score 量纲独立）
    metadata: dict = field(default_factory=dict)
    # ── v2 M1：置信度 + 血统 ──
    confidence: float = 0.5             # 置信度评分 [0, 1]，默认 0.5（未知）
    confidence_factors: dict = field(default_factory=dict)  # 分项因子：{base, freshness, feedback, corroboration}
    provenance: dict = field(default_factory=dict)  # 来源血统：{tool_calls, llm_tokens, trace_run_id, reasoning_summary}
    # ── v2 M2：时效性 / TTL ──
    memory_category: str = "general"    # 记忆类型：quote/event/flow/technical/fundamental/valuation/industry/moat/general
    ttl_days: int = 30                  # 有效天数，-1=永久
    expires_at: str = ""                # ISO 过期时间，空=永久
    last_validated_at: str = ""         # 最后验证时间（手动确认仍有效）
    # ── v2 M3：合并 / 冲突消解 ──
    superseded_by: str = ""             # 被哪条记忆取代（entry_id）
    superseded_at: str = ""             # 取代时间 ISO
    supersede_reason: str = ""          # new_analysis / user_correction / data_refresh
    conflict_with: list = field(default_factory=list)  # 与之冲突的 entry_id 列表


# ------------------------------------------------------------
# 策略接口
# ------------------------------------------------------------
class MemoryBackend(ABC):
    """记忆检索后端抽象基类"""

    @abstractmethod
    def name(self) -> str:
        """后端标识，用于日志和调试"""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """后端是否可用（Embedding 后端可能未配置）"""
        ...

    @abstractmethod
    def add(self, entry: MemoryEntry) -> str:
        """写入一条记忆，返回 entry_id"""
        ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """检索记忆，返回带 score 的条目列表"""
        ...

    @abstractmethod
    def delete(self, entry_id: str) -> bool:
        """删除一条记忆"""
        ...

    @abstractmethod
    def count(self) -> int:
        """总条目数（管理界面用）"""
        ...

    @abstractmethod
    def clean_before(self, date: str) -> int:
        """清理指定日期之前的记忆，返回清理条数"""
        ...

    @abstractmethod
    def clean_by_stock(self, stock_code: str) -> int:
        """清理指定股票的记忆，返回清理条数"""
        ...

    @abstractmethod
    def stats(self) -> dict:
        """返回统计信息：{count, db_size_mb, top_stocks, top_sectors, oldest, newest}"""
        ...

    @abstractmethod
    def list_all(self, limit: int = 50, offset: int = 0) -> List[MemoryEntry]:
        """分页列出所有记忆条目（管理界面用）"""
        ...

    def add_batch(self, entries: List[MemoryEntry]) -> List[str]:
        """批量写入（默认实现逐个写入，子类可覆盖优化）"""
        return [self.add(e) for e in entries]

    def mark_superseded(self, entry_ids: List[str], new_entry_id: str,
                        reason: str = "new_analysis") -> int:
        """仅更新生命周期字段；不支持的后端默认不处理。"""
        return 0

    def clean_expired(self, as_of: str | None = None) -> int:
        """清理已过期条目；不支持的后端默认不处理。"""
        return 0

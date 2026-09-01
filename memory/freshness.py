"""
记忆系统 — 时效性 / TTL / 主动遗忘（v2 M2）

FreshnessManager: 记忆类型分类 + TTL 计算 + 过期检测 + 清理。

9 种记忆分类:
  quote(行情,1天) / event(事件,7天) / flow(资金,3天) / technical(技术,3天)
  / fundamental(基本面,90天) / valuation(估值,30天) / industry(行业地位,365天)
  / moat(护城河,永久) / general(默认,30天)

用法:
    from memory.freshness import FreshnessManager
    fm = FreshnessManager()
    category = fm.classify(entry, extracts)
    ttl = fm.compute_ttl(category)
    entry.memory_category = category
    entry.ttl_days = ttl
    entry.expires_at = fm.compute_expires_at(date_str, ttl)
"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

_log = logging.getLogger(__name__)


class FreshnessManager:
    """记忆时效管理器。

    在 archive() 时分类并设置 TTL；
    在 retrieve() 时过滤或降权过期记忆；
    通过后台任务定期清理。
    """

    # ── 分类关键词 ──────────────────────────────────────────

    # 优先级：tags 关键词 > content 关键词 > extracts 字段 > 默认 general

    TAG_KEYWORDS: Dict[str, str] = {
        # 事件类
        "涨停": "event", "跌停": "event", "异动": "event",
        "利好": "event", "利空": "event", "公告": "event",
        # 资金面
        "资金流入": "flow", "资金流出": "flow", "主力净流入": "flow",
        "主力净流出": "flow", "北向": "flow",
        # 技术类
        "超买": "technical", "超卖": "technical",
        "金叉": "technical", "死叉": "technical",
        "RSI": "technical", "MACD": "technical", "KDJ": "technical",
        # 估值类
        "估值": "valuation", "低估": "valuation", "高估": "valuation",
        "低估值": "valuation", "估值修复": "valuation",
        # 行业地位
        "龙头": "industry", "护城河": "industry", "市占率": "industry",
        "行业领先": "industry", "核心资产": "industry",
    }

    CONTENT_KEYWORDS: Dict[str, str] = {
        # 护城河/壁垒（moat级别，优先级高于 industry）
        "护城河": "moat", "壁垒": "moat", "竞争优势": "moat",
        "品牌优势": "moat", "专利": "moat", "特许经营权": "moat",
        # 行业地位 → industry（不包含护城河级别关键词）
        "龙头": "industry", "市占率": "industry", "行业第一": "industry",
    }

    # extracts 字段 → 分类（用于无 tags 时的兜底）
    EXTRACT_FIELD_CATEGORY: Dict[str, str] = {
        "change_pct": "quote", "price": "quote", "turnover_rate": "quote",
        "pe": "fundamental", "pb": "fundamental", "roe": "fundamental",
        "pe_dynamic": "fundamental", "total_mv": "fundamental",
    }

    # ── TTL 映射 ────────────────────────────────────────────

    TTL_MAP: Dict[str, int] = {
        "quote": 1,          # 行情：1 天
        "event": 7,          # 事件：7 天
        "flow": 3,           # 资金面：3 天
        "technical": 3,      # 技术指标：3 天
        "fundamental": 90,   # 基本面：90 天
        "valuation": 30,     # 估值：30 天
        "industry": 365,     # 行业地位：1 年
        "moat": -1,          # 护城河：永久
        "general": 30,       # 默认：30 天
    }

    # ── 分类 ────────────────────────────────────────────────

    def classify(self, entry=None,
                 extracts: Optional[List[dict]] = None) -> str:
        """推断记忆分类。

        优先级：tags 关键词 > content 关键词 > extracts 字段 > 默认 general
        """
        tags = []
        content = ""
        if entry is not None:
            if hasattr(entry, "metadata") and entry.metadata:
                tags = entry.metadata.get("tags", []) or []
            content = getattr(entry, "content", "") or ""

        # 1. tags 关键词（最高优先级）
        if tags:
            for tag in tags:
                tag_lower = str(tag).lower().replace(" ", "")
                for keyword, cat in self.TAG_KEYWORDS.items():
                    if keyword.lower().replace(" ", "") in tag_lower:
                        return cat

        # 2. content 关键词
        if content:
            content_lower = content.lower().replace(" ", "")
            for keyword, cat in self.CONTENT_KEYWORDS.items():
                if keyword.lower().replace(" ", "") in content_lower:
                    return cat

        # 3. extracts 字段
        if extracts:
            for ex in extracts:
                if not isinstance(ex, dict):
                    continue
                for field, cat in self.EXTRACT_FIELD_CATEGORY.items():
                    if ex.get(field) is not None:
                        return cat

        # 4. 默认
        return "general"

    # ── TTL 计算 ────────────────────────────────────────────

    def compute_ttl(self, category: str) -> int:
        """分类 → TTL 天数，moat 返回 -1（永久）。"""
        return self.TTL_MAP.get(category, 30)

    def compute_expires_at(self, date_iso: str, ttl_days: int) -> str:
        """date_iso + ttl_days → expires_at ISO 字符串。

        ttl_days=-1 返回空字符串（永久有效）。
        """
        if ttl_days < 0:
            return ""
        try:
            if "T" in date_iso:
                date_iso = date_iso[:10]
            dt = datetime.strptime(date_iso, "%Y-%m-%d")
            expires = dt + timedelta(days=ttl_days)
            return expires.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            # 无法解析日期时用当前时间
            expires = datetime.now() + timedelta(days=ttl_days)
            return expires.strftime("%Y-%m-%d")

    # ── 过期判断 ────────────────────────────────────────────

    def is_stale(self, entry) -> bool:
        """判断记忆是否已过期。"""
        expires = ""
        if hasattr(entry, "expires_at"):
            expires = entry.expires_at
        elif isinstance(entry, dict):
            expires = entry.get("expires_at", "")
        if not expires:
            return False  # 空=永久
        today = datetime.now().strftime("%Y-%m-%d")
        return expires < today

    def filter_stale(self, entries: List,
                     mode: str = "exclude") -> List:
        """按过期状态过滤/降权。

        Args:
            entries: MemoryEntry 列表
            mode: "exclude" — 剔除过期条目
                  "degrade" — 保留但 score × 0.3
        Returns:
            过滤后的条目列表
        """
        if mode == "degrade":
            for e in entries:
                if self.is_stale(e):
                    if hasattr(e, "score"):
                        e.score = round(e.score * 0.3, 4)
            return entries
        else:  # exclude
            return [e for e in entries if not self.is_stale(e)]

    # ── 清理 ────────────────────────────────────────────────

    def cleanup_expired(self, db_path: str = "",
                         dry_run: bool = True) -> dict:
        """扫描并清理过期语义记忆。

        Args:
            db_path: stock_memory.db 路径
            dry_run: True=只返回计数，不实际删除
        Returns:
            {"scanned": int, "expired": int, "deleted": int, "dry_run": bool}
        """
        if not db_path:
            import os as _os
            from utils.app_paths import get_stock_memory_db_path
            db_path = get_stock_memory_db_path()
            if not _os.path.exists(db_path):
                return {"scanned": 0, "expired": 0, "deleted": 0, "dry_run": dry_run}

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # 查已过期且非永久的记录
            rows = conn.execute(
                "SELECT entry_id, expires_at FROM semantic_memory_meta "
                "WHERE expires_at IS NOT NULL AND expires_at != '' AND expires_at < ?",
                (today,)
            ).fetchall()
            expired = len(rows)

            if not dry_run and expired > 0:
                ids = [r["entry_id"] for r in rows]
                # 分批删（避免 SQLite 参数过多）
                batch = 500
                for i in range(0, len(ids), batch):
                    chunk = ids[i:i + batch]
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(
                        f"DELETE FROM semantic_memory_fts WHERE entry_id IN ({placeholders})",
                        chunk
                    )
                    conn.execute(
                        f"DELETE FROM semantic_memory_meta WHERE entry_id IN ({placeholders})",
                        chunk
                    )
                conn.commit()

            # 必须在 conn.close() 之前取 scanned，否则会操作已关闭的连接
            scanned = conn.execute("SELECT COUNT(*) FROM semantic_memory_meta").fetchone()[0] \
                if not dry_run else 0
        finally:
            conn.close()

        return {
            "scanned": scanned,
            "expired": expired,
            "deleted": expired if not dry_run else 0,
            "dry_run": dry_run,
        }

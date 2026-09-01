"""
记忆系统 — 情景记忆
记录每次分析的摘要事件，支持"上次说了什么""这周分析了哪些股票"等回溯查询。
存储于 stock_memory.db 的 episode_log 表中。
"""
import json
import uuid
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict


class EpisodicMemory:
    """情景记忆——记录分析事件"""

    def __init__(self, db_path: str):
        self._db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def log(self,
            query: str,
            session_id: Optional[str] = None,
            dialog_uuid: Optional[str] = None,
            topics: Optional[List[str]] = None,
            key_findings: Optional[List[str]] = None,
            conclusion: Optional[str] = None,
            stocks_mentioned: Optional[List[str]] = None,
            sectors_mentioned: Optional[List[str]] = None,
            tags: Optional[List[str]] = None,
            trace_run_id: Optional[str] = None,
            confidence: float = 0.5,          # v2: 置信度
            ) -> str:
        """记录一次分析事件，返回 entry_id"""
        entry_id = uuid.uuid4().hex[:12]
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO episode_log "
            "(id, session_id, dialog_uuid, date, query, topics, key_findings, "
            "conclusion, stocks_mentioned, sectors_mentioned, tags, trace_run_id, "
            "confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, session_id, dialog_uuid,
             datetime.now().isoformat(), query,
             json.dumps(topics or [], ensure_ascii=False),
             json.dumps(key_findings or [], ensure_ascii=False),
             conclusion,
             json.dumps(stocks_mentioned or [], ensure_ascii=False),
             json.dumps(sectors_mentioned or [], ensure_ascii=False),
             json.dumps(tags or [], ensure_ascii=False),
             trace_run_id,
             confidence)
        )
        conn.commit()
        conn.close()
        return entry_id

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """全文检索历史事件（LIKE 模糊匹配）"""
        conn = self._get_conn()
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT id, date, query, topics, key_findings, conclusion, "
            "stocks_mentioned, sectors_mentioned, tags "
            "FROM episode_log "
            "WHERE query LIKE ? OR conclusion LIKE ? OR key_findings LIKE ? "
            "ORDER BY date DESC LIMIT ?",
            (like, like, like, top_k)
        ).fetchall()
        conn.close()
        return [_row_to_dict(r) for r in rows]

    def get_recent(self, days: int = 7, limit: int = 20) -> List[dict]:
        """最近 N 天的事件"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, date, query, topics, key_findings, conclusion, "
            "stocks_mentioned, sectors_mentioned, tags "
            "FROM episode_log "
            "WHERE date >= datetime('now', ?) "
            "ORDER BY date DESC LIMIT ?",
            (f"-{days} days", limit)
        ).fetchall()
        conn.close()
        return [_row_to_dict(r) for r in rows]

    def get_by_stock(self, stock_code: str, limit: int = 20) -> List[dict]:
        """某只股票的分析历史"""
        conn = self._get_conn()
        like = f"%{stock_code}%"
        rows = conn.execute(
            "SELECT id, date, query, topics, key_findings, conclusion, "
            "stocks_mentioned, sectors_mentioned, tags "
            "FROM episode_log "
            "WHERE stocks_mentioned LIKE ? "
            "ORDER BY date DESC LIMIT ?",
            (like, limit)
        ).fetchall()
        conn.close()
        return [_row_to_dict(r) for r in rows]

    def get_by_session(self, session_id: str, limit: int = 50) -> List[dict]:
        """按会话 ID 查询"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, date, query, topics, key_findings, conclusion, "
            "stocks_mentioned, sectors_mentioned, tags "
            "FROM episode_log "
            "WHERE session_id = ? "
            "ORDER BY date DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
        conn.close()
        return [_row_to_dict(r) for r in rows]

    def count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM episode_log").fetchone()
        conn.close()
        return row[0] if row else 0

    def clean_before(self, date: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM episode_log WHERE date < ?", (date,)
        ).fetchone()
        deleted = row[0] if row else 0
        if deleted:
            conn.execute("DELETE FROM episode_log WHERE date < ?", (date,))
            conn.commit()
        conn.close()
        return deleted

    def delete(self, entry_id: str) -> bool:
        conn = self._get_conn()
        conn.execute("DELETE FROM episode_log WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()
        return True


def _row_to_dict(row) -> dict:
    """将 DB 行转为字典，解析 JSON 字段"""
    keys = ["id", "date", "query", "topics", "key_findings",
            "conclusion", "stocks_mentioned", "sectors_mentioned", "tags"]
    d = dict(zip(keys, row))
    for json_field in ("topics", "key_findings", "stocks_mentioned",
                       "sectors_mentioned", "tags"):
        try:
            d[json_field] = json.loads(d[json_field]) if d[json_field] else []
        except (json.JSONDecodeError, TypeError):
            d[json_field] = []
    return d

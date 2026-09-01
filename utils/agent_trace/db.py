import sqlite3
from pathlib import Path


_trace_conn: sqlite3.Connection | None = None


def resolve_trace_db() -> str | None:
    try:
        from utils.app_paths import get_trace_db_path
        db_path = get_trace_db_path()
        if Path(db_path).exists():
            return db_path
    except Exception:
        pass
    fallback = Path("agent_trace.db")
    return str(fallback) if fallback.exists() else None


def get_trace_conn() -> sqlite3.Connection | None:
    global _trace_conn
    db_path = resolve_trace_db()
    if db_path is None:
        return None
    if _trace_conn is None:
        _trace_conn = sqlite3.connect(db_path, check_same_thread=False)
        _trace_conn.row_factory = sqlite3.Row
        _trace_conn.execute("PRAGMA journal_mode=WAL")
        _trace_conn.execute("PRAGMA cache_size=-32768")
    return _trace_conn


def find_latest_trace_run_id() -> str | None:
    conn = get_trace_conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None

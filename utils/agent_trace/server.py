import gzip
import json
import socket
import sqlite3
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from utils.agent_trace.cli import _db_path

_RUNS_LIST_COLS = "id,agent_name,status,created_at,finished_at,duration_ms"
_STATIC_HTML_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
_STATIC_ASSET_CACHE_CONTROL = "private, max-age=600"

_db_conn: sqlite3.Connection | None = None


def _static_cache_control_for_path(path: str) -> str:
    clean_path = path.split("?", 1)[0].split("#", 1)[0]
    filename = clean_path.rsplit("/", 1)[-1]
    if clean_path in ("", "/") or filename.endswith(".html"):
        return _STATIC_HTML_CACHE_CONTROL
    return _STATIC_ASSET_CACHE_CONTROL


def _get_conn(db: str) -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        from utils.agent_trace.models import _ensure_db
        _ensure_db(Path(db))
        _db_conn = sqlite3.connect(db, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA cache_size=-65536")
        _ensure_indexes(_db_conn)
    return _db_conn


def _ensure_indexes(c: sqlite3.Connection):
    c.execute("CREATE INDEX IF NOT EXISTS idx_steps_run_id ON steps(run_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_steps_run_type ON steps(run_id, step_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_step_id ON messages(step_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC)")
    c.commit()


class TraceHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        from utils.app_paths import get_agent_trace_viewer_dir
        viewer_dir = Path(get_agent_trace_viewer_dir())
        super().__init__(*a, directory=str(viewer_dir), **kw)

    def do_GET(self):
        db = _resolved_db
        if self.path == "/api/runs":
            self._json_response(self._get_runs(db))
        elif self.path.startswith("/api/runs/"):
            rid = self.path.split("/api/runs/")[1].split("/")[0]
            sub = self.path.split("/api/runs/")[1].split("/")
            if len(sub) > 1 and sub[1] == "steps":
                self._json_response(self._get_steps(db, rid))
            else:
                self._json_response(self._get_run(db, rid))
        elif self.path.startswith("/api/calendar"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            year = int(qs.get("year", [0])[0]) or None
            month = int(qs.get("month", [0])[0]) or None
            self._json_response(self._get_calendar(db, year, month))
        elif self.path == "/api/db-path":
            self._json_response({"db_path": db})
        else:
            super().do_GET()

    @staticmethod
    def _get_runs(db: str):
        c = _get_conn(db)
        rows = c.execute(
            f"SELECT {_RUNS_LIST_COLS} FROM runs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _get_run(db: str, run_id: str):
        c = _get_conn(db)
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(run) if run else {}

    @staticmethod
    def _get_steps(db: str, run_id: str):
        c = _get_conn(db)
        steps = c.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        if not steps:
            return []
        step_ids = [s["id"] for s in steps]
        placeholders = ",".join("?" * len(step_ids))
        msg_rows = c.execute(
            f"SELECT * FROM messages WHERE step_id IN ({placeholders}) ORDER BY step_id, seq",
            step_ids,
        ).fetchall()
        msg_by_step: dict = {}
        for m in msg_rows:
            msg_by_step.setdefault(m["step_id"], []).append(dict(m))
        result = []
        for s in steps:
            d = dict(s)
            d["messages"] = msg_by_step.get(s["id"], [])
            result.append(d)
        return result

    @staticmethod
    def _get_calendar(db: str, year: int | None, month: int | None):
        """按日期分组返回 run 统计和 token 消耗。"""
        from utils.agent_trace.calendar_query import query_trace_calendar
        c = _get_conn(db)
        return query_trace_calendar(c, year, month)

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        accept_enc = self.headers.get("Accept-Encoding", "")
        use_gzip = "gzip" in accept_enc and len(body) > 1024
        if use_gzip:
            body = gzip.compress(body, compresslevel=6)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", _static_cache_control_for_path(self.path))
        super().end_headers()

    def log_message(self, format, *args):
        pass


def _has_ipv6():
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return True
    except Exception:
        return False


class DualStackHTTPServer(HTTPServer):
    address_family = socket.AF_INET6 if _has_ipv6() else HTTPServer.address_family


_resolved_db = ""


def cmd_serve(args):
    global _resolved_db
    _resolved_db = _db_path(args)
    port = args.port or 8765

    _get_conn(_resolved_db)

    server = DualStackHTTPServer(("::", port), TraceHandler)
    url = f"http://localhost:{port}/viewer.html"
    print(f"🔍 Trace Viewer: {url}")
    print(f"   DB: {_resolved_db}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        global _db_conn
        if _db_conn:
            _db_conn.close()
            _db_conn = None
        server.server_close()

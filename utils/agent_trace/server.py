import json
import sqlite3
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from utils.agent_trace.cli import _conn, _db_path


class TraceHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        viewer_dir = Path(__file__).parent
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
        elif self.path == "/api/db-path":
            self._json_response({"db_path": db})
        else:
            super().do_GET()

    @staticmethod
    def _get_runs(db: str):
        if not Path(db).exists():
            return []
        with _conn(db) as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _get_run(db: str, run_id: str):
        if not Path(db).exists():
            return {}
        with _conn(db) as c:
            run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(run) if run else {}

    @staticmethod
    def _get_steps(db: str, run_id: str):
        if not Path(db).exists():
            return []
        with _conn(db) as c:
            steps = c.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            result = []
            for s in steps:
                d = dict(s)
                msgs = c.execute(
                    "SELECT * FROM messages WHERE step_id=? ORDER BY seq",
                    (s["id"],),
                ).fetchall()
                d["messages"] = [dict(m) for m in msgs]
                result.append(d)
            return result

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


_resolved_db = ""


def cmd_serve(args):
    global _resolved_db
    _resolved_db = _db_path(args)
    port = args.port or 8765

    server = HTTPServer(("0.0.0.0", port), TraceHandler)
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
        server.server_close()

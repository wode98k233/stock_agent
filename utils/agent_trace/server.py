import gzip
import json
import logging
import socket
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from utils.agent_trace.cli import _db_path

_RUNS_LIST_COLS = "id,agent_name,status,created_at,finished_at,duration_ms"
_STATIC_HTML_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
_STATIC_ASSET_CACHE_CONTROL = "private, max-age=600"

logger = logging.getLogger(__name__)


def _static_cache_control_for_path(path: str) -> str:
    clean_path = path.split("?", 1)[0].split("#", 1)[0]
    filename = clean_path.rsplit("/", 1)[-1]
    if clean_path in ("", "/") or filename.endswith(".html"):
        return _STATIC_HTML_CACHE_CONTROL
    return _STATIC_ASSET_CACHE_CONTROL


class TraceHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        from utils.app_paths import get_agent_trace_viewer_dir
        viewer_dir = Path(get_agent_trace_viewer_dir())
        super().__init__(*a, directory=str(viewer_dir), **kw)

    def do_GET(self):
        db = _resolved_db
        if self.path == "/api/runs":
            self._json_response(self._get_runs())
        elif self.path.startswith("/api/runs/"):
            rid = self.path.split("/api/runs/")[1].split("/")[0]
            sub = self.path.split("/api/runs/")[1].split("/")
            if len(sub) > 1 and sub[1] == "steps":
                self._json_response(self._get_steps(rid))
            else:
                self._json_response(self._get_run(rid))
        elif self.path == "/api/reviews" or self.path.startswith("/api/reviews?"):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            run_id = (qs.get("run_id") or [""])[0]
            self._json_response(_reviews_payload(run_id))
        elif self.path.startswith("/api/calendar"):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            year = int(qs.get("year", [0])[0]) or None
            month = int(qs.get("month", [0])[0]) or None
            self._json_response(self._get_calendar(year, month))
        elif self.path == "/api/db-path":
            self._json_response({"db_path": db})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path != "/api/reviews":
            self.send_error(404)
            return
        # 跨域防护最薄一层：本服务无 do_OPTIONS，浏览器对带 JSON 的跨域请求会先发
        # OPTIONS 预检而失败，因此仅接受 application/json 即可阻断外部网页触发付费
        # LLM 调用；监听地址维持既有绑定行为，不在本次变更范围内。
        ctype = self.headers.get("Content-Type", "") or ""
        if ctype.split(";")[0].strip().lower() != "application/json":
            self._json_response({"error": "unsupported media type"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._json_response({"error": "invalid json"})
            return
        self._json_response(_create_review_payload(payload))

    @staticmethod
    def _get_runs():
        from utils.agent_trace.store import store
        runs = store.list_runs(limit=100)
        cols = _RUNS_LIST_COLS.split(",")
        return [{c: r.get(c) for c in cols} for r in runs]

    @staticmethod
    def _get_run(run_id: str):
        from utils.agent_trace.store import store
        return store.get_run(run_id) or {}

    @staticmethod
    def _get_steps(run_id: str):
        from utils.agent_trace.store import store
        return store.get_steps_with_messages(run_id)

    @staticmethod
    def _get_calendar(year: int | None, month: int | None):
        """按日期分组返回 run 统计和 token 消耗（分库：经 store 逐月合并）。"""
        from utils.agent_trace.calendar_query import query_trace_calendar
        from utils.agent_trace.store import store
        return query_trace_calendar(store, year, month)

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


def _reviews_payload(run_id: str) -> dict:
    if not run_id:
        return {"items": []}
    from utils.agent_trace.store import store
    from eval.review import store as review_store
    return {"items": review_store.list_reviews(store.reviews_conn(), "run", run_id)}


def _create_review_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"error": "invalid payload"}
    run_id = str((payload or {}).get("run_id") or "").strip()
    if not run_id:
        return {"error": "run_id 必填"}
    from utils.agent_trace.store import store
    from eval.review import engine
    conn = store.reviews_conn()
    active = engine.find_active_running(conn, "run", run_id)
    if active:
        return {
            "error": "该 run 已有评审进行中",
            "review_uuid": active["review_uuid"],
        }
    review_uuid = engine.create_pending(conn, run_id)
    _start_review_thread(review_uuid, run_id)
    return {"review_uuid": review_uuid, "status": "running"}


def _prewarm_review_engine():
    """后台预加载评审引擎（含 langchain），避免首次 POST 冷加载阻塞。"""
    try:
        from eval.review import engine, store  # noqa: F401
    except Exception:
        pass


def _start_review_thread(review_uuid: str, run_id: str) -> None:
    threading.Thread(
        target=_review_worker, args=(review_uuid, run_id),
        name="trace-review", daemon=True,
    ).start()


def _review_worker(review_uuid: str, run_id: str) -> None:
    try:
        from utils.agent_trace.store import store
        from eval.review import engine
        # reviews 写主库；trace 读取由 engine._trace_conn 按月路由
        engine.execute(store.reviews_conn(), review_uuid, run_id)
    except Exception:
        logger.warning("[review] worker 异常 uuid=%s", review_uuid, exc_info=True)
        try:
            from utils.agent_trace.store import store
            from eval.review import store as review_store
            review_store.fail_review(store.reviews_conn(), review_uuid, "worker 异常")
        except Exception:
            logger.warning(
                "[review] 标记失败记录也失败 uuid=%s", review_uuid, exc_info=True
            )


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

    # 后台预热评审引擎：避免首次 POST 在单线程 HTTPServer 内同步冷加载
    # langchain（秒级~十秒级）造成全局阻塞
    threading.Thread(
        target=_prewarm_review_engine, daemon=True, name="review-prewarm",
    ).start()

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
        server.server_close()

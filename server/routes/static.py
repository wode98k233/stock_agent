"""静态资源路由：/, /favicon.ico, /logs/{filename}。"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse, Response

from server.cache_headers import STATIC_HTML_CACHE_CONTROL

router = APIRouter()


def _get_static_dir() -> Path:
    from utils.app_paths import get_server_static_dir
    return Path(get_server_static_dir())


@router.get("/")
async def index():
    static_dir = _get_static_dir()
    response = FileResponse(static_dir / "index.html")
    response.headers["Cache-Control"] = STATIC_HTML_CACHE_CONTROL
    return response


@router.get("/favicon.ico")
async def favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">📈</text></svg>'
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/logs/{filename:path}")
async def serve_log(filename: str):
    from utils.app_paths import get_logs_dir

    logs_dir = Path(get_logs_dir()).resolve()
    file_path = (logs_dir / filename).resolve()
    try:
        file_path.relative_to(logs_dir)
    except ValueError:
        return PlainTextResponse("非法路径", status_code=400)
    if not file_path.is_file():
        return PlainTextResponse("日志文件不存在", status_code=404)
    return FileResponse(file_path)

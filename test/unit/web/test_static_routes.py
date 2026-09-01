"""静态资源路由的路径边界测试。"""
import asyncio
from pathlib import Path

from fastapi.responses import FileResponse, PlainTextResponse


def _serve_log(filename: str):
    from server.routes.static import serve_log

    return asyncio.run(serve_log(filename))


def test_serve_log_allows_nested_file(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    log_file = logs_dir / "2026-07-14" / "app.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("ok", encoding="utf-8")
    monkeypatch.setattr("utils.app_paths.get_logs_dir", lambda: str(logs_dir))

    response = _serve_log("2026-07-14/app.log")

    assert isinstance(response, FileResponse)
    assert Path(response.path).resolve() == log_file.resolve()


def test_serve_log_rejects_absolute_path_outside_logs(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr("utils.app_paths.get_logs_dir", lambda: str(logs_dir))

    response = _serve_log(str(outside))

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 400


def test_serve_log_rejects_backslash_traversal(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr("utils.app_paths.get_logs_dir", lambda: str(logs_dir))

    response = _serve_log(r"..\outside.log")

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 400


def test_serve_log_rejects_directory(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    nested_dir = logs_dir / "2026-07-14"
    nested_dir.mkdir(parents=True)
    monkeypatch.setattr("utils.app_paths.get_logs_dir", lambda: str(logs_dir))

    response = _serve_log("2026-07-14")

    assert isinstance(response, PlainTextResponse)
    assert response.status_code == 404


"""
打包路径与统一入口测试。

目标：覆盖 PyInstaller onedir 模式下的内置资源读取路径，以及 launcher 的多入口分发。
"""
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_resource_paths_use_meipass_when_frozen(monkeypatch, tmp_path):
    """打包模式下静态资源、模板和 skills 应从 _MEIPASS 读取。"""
    import utils.app_paths as app_paths

    exe_dir = tmp_path / "dist" / "stock-radar"
    exe_dir.mkdir(parents=True)
    exe_path = exe_dir / "stock-radar.exe"
    exe_path.write_text("", encoding="utf-8")
    resource_root = tmp_path / "_MEI12345"
    resource_root.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))
    monkeypatch.setattr(sys, "_MEIPASS", str(resource_root), raising=False)

    assert app_paths.get_app_dir() == str(exe_dir)
    assert app_paths.get_resource_root() == str(resource_root)
    assert app_paths.get_skills_root() == str(resource_root)
    assert app_paths.get_template_dir() == str(resource_root / "agents" / "report_templates")
    assert app_paths.get_server_static_dir() == str(resource_root / "server" / "static")
    assert app_paths.get_agent_trace_viewer_dir() == str(resource_root / "utils" / "agent_trace")
    assert app_paths.get_db_path() == str(exe_dir / "stock_radar.db")
    assert app_paths.get_trace_db_path() == str(exe_dir / "agent_trace.db")


def test_resource_paths_use_repo_root_in_dev_mode(monkeypatch):
    """开发模式下资源路径仍指向项目源码目录。"""
    import utils.app_paths as app_paths

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    if hasattr(sys, "_MEIPASS"):
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    root = Path(__file__).resolve().parents[3]

    assert Path(app_paths.get_resource_root()) == root
    assert Path(app_paths.get_server_static_dir()) == root / "server" / "static"
    assert Path(app_paths.get_agent_trace_viewer_dir()) == root / "utils" / "agent_trace"
    assert Path(app_paths.get_template_dir()) == root / "agents" / "report_templates"


def test_pyinstaller_spec_uses_project_relative_source_paths():
    """main.spec 应基于 spec 文件位置定位项目资源，避免写入本机绝对路径。"""
    root = Path(__file__).resolve().parents[3]
    spec_text = (root / "build_spec" / "main.spec").read_text(encoding="utf-8")

    assert "project_root" in spec_text
    assert "SPECPATH" in spec_text
    assert not re.search(r"[A-Za-z]:[\\/]", spec_text)


def test_pyinstaller_spec_declares_three_launcher_exes():
    """打包产物应包含 CLI、Web、Trace 三个启动器。"""
    root = Path(__file__).resolve().parents[3]
    spec_text = (root / "build_spec" / "main.spec").read_text(encoding="utf-8")

    assert re.search(r"name=['\"]stock-radar['\"]", spec_text)
    assert re.search(r"name=['\"]stock-radar-web['\"]", spec_text)
    assert re.search(r"name=['\"]stock-radar-trace['\"]", spec_text)
    assert spec_text.count("EXE(") >= 3


def test_plain_console_sanitizes_unsupported_icons(monkeypatch):
    """打包控制台可把 emoji 和装饰符号降级为普通字符，保留中文。"""
    monkeypatch.setenv("STOCK_RADAR_PLAIN_CONSOLE", "1")

    from utils.console import sanitize_console_text

    text = sanitize_console_text("📊 分析结果 ✅ ⚠️ ║ ─ ━ →")

    assert "分析结果" in text
    assert "📊" not in text
    assert "✅" not in text
    assert "⚠" not in text
    assert "║" not in text
    assert "─" not in text
    assert "━" not in text
    assert "[OK]" in text
    assert "[WARN]" in text
    assert "|" in text
    assert "-" in text
    assert "->" in text


def test_server_app_mounts_static_from_app_paths(monkeypatch, tmp_path):
    """FastAPI 静态目录应使用 app_paths，而不是 server/app.py 的 __file__。"""
    from server import app as server_app

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    captured = {}

    class FakeFastAPI:
        def __init__(self, *args, **kwargs):
            self.state = MagicMock(web=None)

        def mount(self, path, app, name=None):
            captured["mount"] = {"path": path, "app": app, "name": name}

        def include_router(self, router, **kwargs):
            pass

        def add_middleware(self, middleware_class, **kwargs):
            pass

        def get(self, path):
            def deco(fn):
                return fn
            return deco

        def post(self, path):
            def deco(fn):
                return fn
            return deco

        def put(self, path):
            def deco(fn):
                return fn
            return deco

        def delete(self, path):
            def deco(fn):
                return fn
            return deco

        def patch(self, path):
            def deco(fn):
                return fn
            return deco

    class FakeStaticFiles:
        def __init__(self, directory):
            captured["static_directory"] = directory

    class FakeBaseModel:
        pass

    monkeypatch.setattr(server_app, "_require_fastapi", lambda: (
        FakeFastAPI,
        Exception,
        object,
        FakeStaticFiles,
        FakeBaseModel,
    ))
    monkeypatch.setattr("utils.app_paths.get_server_static_dir", lambda: str(static_dir))

    server_app.create_app(state=MagicMock())

    assert captured["mount"]["path"] == "/static"
    assert captured["mount"]["name"] == "static"
    assert captured["static_directory"] == str(static_dir)


def test_server_static_uses_html_revalidation_and_immutable_versioned_assets(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from starlette.middleware.gzip import GZipMiddleware

    from server.app import create_app

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (static_dir / "app.js").write_text("const payload = '%s';" % ("x" * 4096), encoding="utf-8")

    class Runtime:
        async def shutdown(self):
            pass

    monkeypatch.setattr("utils.app_paths.get_server_static_dir", lambda: str(static_dir))

    app = create_app(state=MagicMock(runtime=Runtime()))
    assert any(m.cls is GZipMiddleware for m in app.user_middleware)

    with TestClient(app) as client:
        index_res = client.get("/")
        versioned_asset_res = client.get("/static/app.js?v=test", headers={"Accept-Encoding": "gzip"})
        unversioned_asset_res = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})

    assert index_res.status_code == 200
    assert index_res.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"

    assert versioned_asset_res.status_code == 200
    assert versioned_asset_res.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "Accept-Encoding" in versioned_asset_res.headers.get("vary", "")

    assert unversioned_asset_res.status_code == 200
    assert unversioned_asset_res.headers["cache-control"] == "private, max-age=600"


def test_launcher_dispatches_cli_web_and_trace(monkeypatch):
    """launcher 应支持 cli / web / trace 三个入口，并保持默认 cli。"""
    import launcher

    calls = []
    cli_main = MagicMock(side_effect=lambda args=None: calls.append("cli"))
    web_main = MagicMock(side_effect=lambda: calls.append("web"))
    trace_main = MagicMock(side_effect=lambda: calls.append(("trace", list(sys.argv))))

    monkeypatch.setattr(launcher, "_run_cli", cli_main)
    monkeypatch.setattr(launcher, "_run_web", web_main)
    monkeypatch.setattr(launcher, "_run_trace", trace_main)

    with patch.object(sys, "argv", ["stock-radar.exe"]):
        launcher.main()
    with patch.object(sys, "argv", ["stock-radar.exe", "cli"]):
        launcher.main()
    with patch.object(sys, "argv", ["stock-radar.exe", "web"]):
        launcher.main()
    with patch.object(sys, "argv", ["stock-radar.exe", "trace", "ls"]):
        launcher.main()
    with patch.object(sys, "argv", ["stock-radar-web.exe"]):
        launcher.main()
    with patch.object(sys, "argv", ["stock-radar-trace.exe"]):
        launcher.main()

    assert calls[0] == "cli"
    assert calls[1] == "cli"
    assert calls[2] == "web"
    assert calls[3] == ("trace", ["stock-radar.exe", "ls"])
    assert calls[4] == "web"
    assert calls[5] == ("trace", ["stock-radar-trace.exe", "serve"])

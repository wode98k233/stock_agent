"""skill-manager 工具测试按钮回归：tool meta 不得内嵌 HTML 属性（引号截断） + 资源版本递增。"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _skill_manager_js() -> str:
    return (ROOT / "server" / "static" / "js" / "skill-manager.js").read_text(encoding="utf-8")


def test_tool_meta_not_embedded_in_html_attribute():
    js = _skill_manager_js()
    assert "data-tool='" not in js
    assert "JSON.parse(btn.dataset.tool)" not in js
    assert "data-tool-idx" in js
    assert "toolMetas[Number(btn.dataset.toolIdx)]" in js


def test_script_version_bumped():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    assert "skill-manager.js?v=4" in html


def test_script_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(ROOT / "server" / "static" / "js" / "skill-manager.js")],
        cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr

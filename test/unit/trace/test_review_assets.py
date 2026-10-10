"""前端接线静态断言（与 test_trace_static_assets.py 同风格）。"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_review_drawer_wired_in_viewer():
    viewer = (ROOT / "utils" / "agent_trace" / "viewer.html").read_text(encoding="utf-8")
    assert 'href="css/reviews.css"' in viewer
    assert 'src="js/reviews.js"' in viewer
    assert 'id="reviewDrawer"' in viewer


def test_review_entry_in_run_list():
    render_js = (ROOT / "utils" / "agent_trace" / "js" / "render.js").read_text(encoding="utf-8")
    assert "openReview('${r.id}')" in render_js
    assert "event.stopPropagation()" in render_js


def test_review_api_functions():
    api_js = (ROOT / "utils" / "agent_trace" / "js" / "api.js").read_text(encoding="utf-8")
    reviews_js = (ROOT / "utils" / "agent_trace" / "js" / "reviews.js").read_text(encoding="utf-8")
    assert "/api/reviews" in api_js
    assert "function fetchReviews" in api_js
    assert "function createReview" in api_js
    assert "function openReview" in reviews_js
    assert "function submitReview" in reviews_js
    assert "setInterval(refreshReviews" in reviews_js


def test_review_scripts_are_valid_javascript():
    for name in ("reviews.js", "api.js", "render.js"):
        result = subprocess.run(
            ["node", "--check", str(ROOT / "utils" / "agent_trace" / "js" / name)],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"

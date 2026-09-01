from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "server" / "static"


def test_status_player_is_wired_as_buffered_frontend_stream():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    player_js = (STATIC_DIR / "js" / "status-player.js").read_text(encoding="utf-8")

    assert "/static/js/status-player.js" in index_html
    assert index_html.index("/static/js/status-player.js") < index_html.index("/static/js/app.js")
    assert "StatusPlayer" in app_js
    assert "statusPlayer.start()" in app_js
    assert "statusPlayer.enqueue" in app_js
    assert "stopStatusPlayer" in app_js
    assert "statusPlayer.stop()" in app_js
    assert "chat.updateStatusBubble" not in app_js
    assert "MIN_STEP_DELAY" in player_js
    assert "MAX_STEP_DELAY" in player_js
    assert "IDLE_MESSAGES" in player_js
    assert "typewriterStatusLine" in player_js
    assert "pending-only playback" in player_js

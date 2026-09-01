from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "server" / "static"


def test_copy_image_static_assets_are_wired():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    chat_export_js = (STATIC_DIR / "js" / "chat-export.js").read_text(encoding="utf-8")
    chat_core_js = (STATIC_DIR / "js" / "chat-core.js").read_text(encoding="utf-8")
    utils_js = (STATIC_DIR / "js" / "utils.js").read_text(encoding="utf-8")
    chat_css = (STATIC_DIR / "css" / "chat.css").read_text(encoding="utf-8")

    assert "/static/lib/html-to-image.js" not in index_html
    assert "/static/lib/html-to-image.js" in chat_export_js
    assert "loadScriptOnce" in chat_export_js
    assert "loadScriptOnce" in utils_js
    assert "copyBubbleAsImage" in chat_export_js
    assert "navigator.clipboard.write" in chat_export_js
    assert "ClipboardItem" in chat_export_js
    assert "downloadBlob" in chat_export_js
    assert "msg-copy-image-btn" in chat_core_js
    assert ".msg-copy-image-btn" in chat_css


def test_heavy_markdown_libraries_are_lazy_loaded():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    chat_markdown_js = (STATIC_DIR / "js" / "chat-markdown.js").read_text(encoding="utf-8")
    chat_core_js = (STATIC_DIR / "js" / "chat-core.js").read_text(encoding="utf-8")

    assert "/static/lib/mermaid.min.js" not in index_html
    assert "/static/lib/highlight.min.js" not in index_html
    assert "/static/lib/mermaid.min.js" in chat_markdown_js
    assert "/static/lib/highlight.min.js" in chat_markdown_js
    assert "_highlightCodeBlocks" in chat_markdown_js
    assert "_highlightCodeBlocks(mdDiv)" in chat_core_js


def test_dynamic_loader_keeps_global_script_order():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    utils_pos = index_html.index("/static/js/utils.js")
    markdown_pos = index_html.index("/static/js/chat-markdown.js")
    export_pos = index_html.index("/static/js/chat-export.js")
    core_pos = index_html.index("/static/js/chat-core.js")

    assert utils_pos < markdown_pos < core_pos
    assert utils_pos < export_pos < core_pos


def test_eager_static_libs_use_versioned_urls():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "/static/lib/marked.min.js?v=" in index_html
    assert "/static/lib/github-dark.min.css?v=" in index_html

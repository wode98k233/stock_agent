"""对话图片上传 / 转述 / 清理（不联网，LLM 与目录均 mock）。"""
import base64
import io
import os
import time

import pytest
from PIL import Image


def _png_bytes(color=(200, 30, 30), size=(12, 12)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def uploads_dir(tmp_path, monkeypatch):
    import server.uploads as up

    monkeypatch.setattr(up, "get_uploads_dir", lambda: str(tmp_path))
    return tmp_path


def test_save_and_read_image(uploads_dir):
    import server.uploads as up

    meta = up.save_image(_b64(_png_bytes()), "image/png", "a.png")
    assert len(meta["id"]) == 16
    assert meta["url"] == f"/api/uploads/{meta['id']}"
    got = up.read_image(meta["id"])
    assert got is not None and got[1] == "image/png"
    assert got[0] == _png_bytes()


def test_save_rejects_bad_input(uploads_dir):
    import server.uploads as up

    with pytest.raises(ValueError):
        up.save_image(_b64(_png_bytes()), "image/gif", "a.gif")
    with pytest.raises(ValueError):
        up.save_image("not-base64!!!", "image/png", "a.png")
    with pytest.raises(ValueError):
        up.save_image(_b64(b"hello world"), "image/png", "a.png")


def test_read_image_rejects_traversal(uploads_dir):
    import server.uploads as up

    assert up.read_image("../../etc/passwd") is None
    assert up.read_image("nothex") is None


def test_cleanup_deletes_orphans_after_a_day(uploads_dir, monkeypatch):
    import server.uploads as up

    meta = up.save_image(_b64(_png_bytes()), "image/png", "a.png")
    path = up.find_file(meta["id"])
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    monkeypatch.setattr(up, "referenced_ids", lambda: set())
    res = up.cleanup()
    assert res["deleted"] == 1
    assert up.find_file(meta["id"]) is None


def test_cleanup_keeps_referenced_within_retention(uploads_dir, monkeypatch):
    import server.uploads as up

    meta = up.save_image(_b64(_png_bytes()), "image/png", "a.png")
    monkeypatch.setattr(up, "referenced_ids", lambda: {meta["id"]})
    res = up.cleanup(retention_days=45)
    assert res["deleted"] == 0
    assert up.find_file(meta["id"]) is not None


def test_cleanup_deletes_expired_referenced(uploads_dir, monkeypatch):
    import server.uploads as up

    meta = up.save_image(_b64(_png_bytes()), "image/png", "a.png")
    path = up.find_file(meta["id"])
    old = time.time() - 60 * 86400
    os.utime(path, (old, old))
    monkeypatch.setattr(up, "referenced_ids", lambda: {meta["id"]})
    res = up.cleanup(retention_days=45)
    assert res["deleted"] == 1


def test_caption_bytes_sends_image_part(monkeypatch):
    import server.vision_service as vs

    captured = {}

    class _Resp:
        content = "K线；600519；结论：上升趋势"

    class _LLM:
        def invoke(self, messages):
            captured["messages"] = messages
            return _Resp()

    import utils.llm_factory as factory

    monkeypatch.setattr(factory, "get_llm", lambda: _LLM())
    text = vs.caption_bytes(_png_bytes(), "image/png")
    assert "600519" in text
    parts = captured["messages"][0].content
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_storage_attachments_roundtrip(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="t")
    msg = storage.create_message(
        dialog_uuid=dialog["dialog_uuid"], role="user", content="问题",
        status="completed", attachments='[{"id": "abc", "name": "a.png"}]')
    assert msg["attachments"] == '[{"id": "abc", "name": "a.png"}]'
    listed = storage.list_messages(dialog["dialog_uuid"])
    assert listed[0]["attachments"] == '[{"id": "abc", "name": "a.png"}]'

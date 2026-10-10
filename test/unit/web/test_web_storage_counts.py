"""WebStorage 区间计数单测（真实临时库）。"""
from server.storage import WebStorage


def test_count_dialogs_since(tmp_path):
    s = WebStorage(tmp_path / "web.db")
    s.create_dialog("t1", "react_stock")
    s.create_dialog("t2", "react_stock")
    assert s.count_dialogs_since("2000-01-01 00:00:00") == 2
    assert s.count_dialogs_since("2999-01-01 00:00:00") == 0


def test_count_messages_since(tmp_path):
    s = WebStorage(tmp_path / "web.db")
    d = s.create_dialog("t1", "react_stock")
    s._conn.execute(
        "INSERT INTO web_messages (message_uuid, dialog_uuid, role, content, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("m1", d["dialog_uuid"], "user", "hi", "done",
         "2026-09-01 10:00:00", "2026-09-01 10:00:00"))
    s._conn.commit()
    assert s.count_messages_since("2026-09-01 00:00:00") == 1
    assert s.count_messages_since("2026-09-02 00:00:00") == 0

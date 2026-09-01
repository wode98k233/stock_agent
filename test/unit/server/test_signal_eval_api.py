"""signal_eval API 测试：异步 rescan 任务。"""
import time

from server.routes import signal_eval as se


def test_rescan_starts_and_reports_progress(monkeypatch):
    called = {"scan": False}

    def fake_scan():
        called["scan"] = True
        return 3

    monkeypatch.setattr(se, "scan_messages", fake_scan)
    se._rescan_progress = {}
    task_id = se.start_rescan()
    assert task_id
    # 等线程跑完
    for _ in range(100):
        if se._rescan_progress.get(task_id, {}).get("status") == "completed":
            break
        time.sleep(0.05)
    assert se._rescan_progress[task_id]["status"] == "completed"
    assert se._rescan_progress[task_id]["added"] == 3
    assert called["scan"]


def test_rescan_failed_reports_error(monkeypatch):
    def boom():
        raise RuntimeError("scan failed")

    monkeypatch.setattr(se, "scan_messages", boom)
    se._rescan_progress = {}
    task_id = se.start_rescan()
    for _ in range(100):
        if se._rescan_progress.get(task_id, {}).get("status") in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert se._rescan_progress[task_id]["status"] == "failed"
    assert "scan failed" in se._rescan_progress[task_id]["error"]

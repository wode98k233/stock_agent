"""/api/data-monitor 路由测试"""
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from server.app import create_app


def _mock_dbm(monkeypatch):
    import utils.data_monitor as dbm
    report = {
        "scanned_at": "2026-09-08 09:00",
        "assets": [{"key": "db/x.db", "category": "db", "name": "x.db", "size_bytes": 2048,
                    "record_count": 5, "mtime": "2026-09-08", "tables": []}],
    }
    monkeypatch.setattr(dbm, "scan", lambda: report)
    monkeypatch.setattr(dbm, "overview", lambda: report)
    monkeypatch.setattr(dbm, "trend", lambda days=30: [{"d": "2026-09-01", "category": "db",
                                                        "size_bytes": 1024, "record_count": 2}])
    monkeypatch.setattr(dbm, "cleanup_preview", lambda: [
        {"id": "small_logs", "name": "小日志", "count": 3, "size": 1024, "items": []}])
    monkeypatch.setattr(dbm, "cleanup_run", lambda ids: {"small_logs": {"removed_count": 3, "freed_bytes": 1024}})


def test_api_endpoints(monkeypatch):
    _mock_dbm(monkeypatch)
    app = create_app(state=MagicMock(runtime=AsyncMock()))
    with TestClient(app) as client:
        r = client.get("/api/data-monitor/overview")
        assert r.status_code == 200
        assert r.json()["assets"][0]["key"] == "db/x.db"

        # 默认是后台触发；要同步拿结果得显式 ?wait=1 —— 这条断言要的就是同步报告，
        # 所以必须带上它，否则会随「进程里有没有注册过后台巡检」漂移。
        r = client.post("/api/data-monitor/scan?wait=1")
        assert r.status_code == 200
        assert r.json()["scanned_at"] == "2026-09-08 09:00"

        r = client.get("/api/data-monitor/trend?days=30")
        assert r.status_code == 200
        assert r.json()[0]["category"] == "db"

        r = client.get("/api/data-monitor/cleanup/preview")
        assert r.status_code == 200
        assert r.json()[0]["id"] == "small_logs"

        r = client.post("/api/data-monitor/cleanup/run", json={"rule_ids": ["small_logs"]})
        assert r.status_code == 200
        assert r.json()["small_logs"]["removed_count"] == 3


def _stub_inspector(monkeypatch, fn):
    """把进程级巡检单例换成全新的、只含 storage_scan 的 manager。"""
    import server.background_scans as bs
    import server.scan_jobs as sj
    from server.background_scans import ScanJob, ScanManager

    mgr = ScanManager()
    mgr.register(ScanJob(key="storage_scan", name="存储监控扫描", fn=fn))
    monkeypatch.setattr(bs, "get_manager", lambda: mgr)
    monkeypatch.setattr(sj, "register_default_jobs", lambda manager=None: ["storage_scan"])
    return mgr


def test_scan_defaults_to_background_when_inspector_available(monkeypatch):
    """有后台巡检时默认不阻塞请求：立即返回 accepted，进度走 /api/scans/status。"""
    _mock_dbm(monkeypatch)
    _stub_inspector(monkeypatch, lambda: {"summary": "扫了 1 个资产", "assets": 1})

    app = create_app(state=MagicMock(runtime=AsyncMock()))
    with TestClient(app) as client:
        r = client.post("/api/data-monitor/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert "scanned_at" not in body        # 不是扫描报告，而是「已转后台」回执


def test_scan_running_is_not_triggered_twice(monkeypatch):
    """单飞：手动点两次不会让同一个存储扫描跑两遍。"""
    import threading

    _mock_dbm(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(5)
        return {"summary": "done"}

    mgr = _stub_inspector(monkeypatch, slow)
    t = threading.Thread(target=mgr.run, args=("storage_scan",), daemon=True)
    t.start()
    assert entered.wait(3)

    app = create_app(state=MagicMock(runtime=AsyncMock()))
    with TestClient(app) as client:
        body = client.post("/api/data-monitor/scan").json()
    assert body["accepted"] is False
    assert body["running"] is True
    release.set()
    t.join(5)


def test_scan_falls_back_to_sync_without_inspector(monkeypatch):
    """没有后台巡检（未跑 bootstrap 的场景）→ 退回同步，不能变成什么都没做。"""
    _mock_dbm(monkeypatch)
    import server.background_scans as bs
    from server.background_scans import ScanManager

    monkeypatch.setattr(bs, "get_manager", lambda: ScanManager())   # 空 manager
    app = create_app(state=MagicMock(runtime=AsyncMock()))
    with TestClient(app) as client:
        body = client.post("/api/data-monitor/scan").json()
    assert body["scanned_at"] == "2026-09-08 09:00"
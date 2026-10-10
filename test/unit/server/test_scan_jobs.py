"""四个巡检作业的业务实现测试（全离线：桩掉数据源/引擎/服务层）。

作业的契约是「自己吞掉局部失败，只把整体失败留给框架」——
单个台账定义坏掉、单个资产读不了，都不能让整轮作业记成失败。
"""
import pytest


@pytest.fixture
def scans_on(monkeypatch):
    from config import Config
    for name in ("BACKGROUND_SCANS_ENABLED", "SCAN_STORAGE_ENABLED",
                 "SCAN_THESIS_ENABLED", "SCAN_CROWDING_ENABLED",
                 "SCAN_SHORTLINE_ENABLED"):
        monkeypatch.setattr(Config, name, True)
    return Config


class TestStorageScan:
    def test_summary_shape(self, monkeypatch):
        import utils.data_monitor as dbm
        from server import scan_jobs

        monkeypatch.setattr(dbm, "scan", lambda: {
            "scanned_at": "2026-09-17 21:00",
            "assets": [
                {"key": "db/a.db", "size_bytes": 2 * 1024 * 1024, "record_count": 100},
                {"key": "db/b.db", "size_bytes": 1024, "record_count": 0,
                 "error": "权限不足"},
            ]})
        out = scan_jobs.storage_scan()
        assert out["assets"] == 2
        assert out["errors"] == 1
        assert out["records"] == 100
        assert out["size_mb"] == pytest.approx(2.0, abs=0.01)
        assert "2 个数据资产" in out["summary"]
        assert "1 个读取失败" in out["summary"]
        assert out["scanned_at"] == "2026-09-17 21:00"

    def test_clean_scan_says_all_readable(self, monkeypatch):
        import utils.data_monitor as dbm
        from server import scan_jobs

        monkeypatch.setattr(dbm, "scan", lambda: {
            "assets": [{"key": "db/a.db", "size_bytes": 1024, "record_count": 1}]})
        out = scan_jobs.storage_scan()
        assert out["errors"] == 0
        assert "全部可读" in out["summary"]

    def test_human_readable_size_switches_to_gb(self):
        from server.scan_jobs import _mb
        assert _mb(0) == "0.0 MB"
        assert _mb(3 * 1024 * 1024) == "3.0 MB"
        assert _mb(2 * 1024 ** 3) == "2.00 GB"


class TestThesisRefresh:
    def test_sums_snapshots_and_alerts(self, monkeypatch):
        from server import scan_jobs, thesis_service

        monkeypatch.setattr("thesis.defs.list_defs", lambda: ["a", "b"])

        def _refresh(tid):
            return {"snapshot": {"trade_date": "2026-09-17", "total": 4,
                                 "verdict": "中性偏有利"},
                    "new_alert_count": 2 if tid == "a" else 0,
                    "snapshot_created": tid == "a"}

        monkeypatch.setattr(thesis_service, "refresh", _refresh)
        out = scan_jobs.thesis_refresh()
        assert out["theses"] == 2 and out["refreshed"] == 2
        assert out["new_points"] == 1 and out["alerts"] == 2
        assert out["failed"] == 0
        assert "刷新 2/2 个台账" in out["summary"]
        assert [it["thesis_id"] for it in out["items"]] == ["a", "b"]

    def test_single_broken_definition_does_not_kill_the_round(self, monkeypatch):
        from server import scan_jobs, thesis_service

        monkeypatch.setattr("thesis.defs.list_defs", lambda: ["good", "bad"])

        def _refresh(tid):
            if tid == "bad":
                raise ValueError("台账定义不可用：[x] 阈值 a 未覆盖低端")
            return {"snapshot": {"trade_date": "2026-09-17", "total": 1},
                    "new_alert_count": 0, "snapshot_created": True}

        monkeypatch.setattr(thesis_service, "refresh", _refresh)
        out = scan_jobs.thesis_refresh()
        assert out["refreshed"] == 1 and out["failed"] == 1
        assert "1 个失败" in out["summary"]
        assert any("未覆盖低端" in f for f in out["failures"])

    def test_all_broken_still_returns_report(self, monkeypatch):
        from server import scan_jobs, thesis_service

        monkeypatch.setattr("thesis.defs.list_defs", lambda: ["bad"])
        monkeypatch.setattr(thesis_service, "refresh",
                            lambda tid: (_ for _ in ()).throw(RuntimeError("库坏了")))
        out = scan_jobs.thesis_refresh()
        assert out["refreshed"] == 0 and out["failed"] == 1
        assert out["alerts"] == 0

    def test_no_definitions_is_not_an_error(self, monkeypatch):
        from server import scan_jobs

        monkeypatch.setattr("thesis.defs.list_defs", lambda: [])
        out = scan_jobs.thesis_refresh()
        assert out["theses"] == 0 and out["refreshed"] == 0
        assert "0/0" in out["summary"]

    def test_failures_list_is_capped(self, monkeypatch):
        from server import scan_jobs, thesis_service

        monkeypatch.setattr("thesis.defs.list_defs", lambda: ["t%d" % i for i in range(9)])
        monkeypatch.setattr(thesis_service, "refresh",
                            lambda tid: (_ for _ in ()).throw(RuntimeError("x")))
        out = scan_jobs.thesis_refresh()
        assert out["failed"] == 9
        assert len(out["failures"]) == 5     # 状态里只放前 5 条，避免把 state 撑大


class TestCrowdingScan:
    REPORT = {
        "trade_date": "2026-09-17", "pool_size": 516, "computed_count": 3,
        "hot_count": 1, "insufficient_count": 495, "stale_count": 18,
        "collected_count": 152, "elapsed_ms": 4200,
        "hot": [{"name": "半导体", "score": 88.5, "key": "board:industry_半导体"}],
        "warnings": ["板块池有 496 个待补数，本轮先补 150 个"],
    }

    def _stub(self, monkeypatch, report=None):
        rep = dict(self.REPORT if report is None else report)

        class _Eng:
            def scan(self):
                return dict(rep)

        class _Store:
            @classmethod
            def instance(cls):
                return cls()

            def save_scan(self, payload):
                return {"created": True,
                        "item_count": len(payload.get("items") or [])}

        monkeypatch.setattr("crowding.engine.CrowdingEngine", lambda *a, **k: _Eng())
        monkeypatch.setattr("crowding.storage.CrowdingStorage", _Store)

    def test_summary_and_metrics(self, monkeypatch):
        from server import scan_jobs
        self._stub(monkeypatch)
        out = scan_jobs.crowding_scan()
        assert out["pool_size"] == 516 and out["computed"] == 3
        assert out["hot_count"] == 1 and out["insufficient"] == 495
        assert out["collected"] == 152 and out["created"] is True
        assert out["top"] == "半导体 88.5"
        assert "池 516" in out["summary"] and "补数 152" in out["summary"]
        assert "待补数 495" in out["summary"]

    def test_no_hot_target_omits_top(self, monkeypatch):
        from server import scan_jobs
        rep = dict(self.REPORT, hot=[], hot_count=0)
        self._stub(monkeypatch, rep)
        out = scan_jobs.crowding_scan()
        assert "top" not in out

    def test_no_insufficient_drops_the_note(self, monkeypatch):
        from server import scan_jobs
        rep = dict(self.REPORT, insufficient_count=0)
        self._stub(monkeypatch, rep)
        out = scan_jobs.crowding_scan()
        assert "待补数" not in out["summary"]

    def test_engine_exception_propagates_to_framework(self, monkeypatch):
        """整体失败要抛出去让框架记 last_ok=False —— 吞掉会让人以为扫描成功了。"""
        from server import scan_jobs

        class _Eng:
            def scan(self):
                raise RuntimeError("market_data 库读不了")

        monkeypatch.setattr("crowding.engine.CrowdingEngine", lambda *a, **k: _Eng())
        with pytest.raises(RuntimeError):
            scan_jobs.crowding_scan()


class TestShortlineRefresh:
    def _stub(self, monkeypatch, overview):
        from server import shortline_service as sl
        monkeypatch.setattr(sl, "build_overview", lambda *a, **k: dict(overview))

    def test_summary_shape(self, monkeypatch):
        from server import scan_jobs
        self._stub(monkeypatch, {
            "summary": {"trade_date": "2026-09-21", "score": 62, "level": "warm",
                        "limit_up_count": 88, "limit_down_count": 3, "broken_count": 20},
            "degraded": [],
        })
        out = scan_jobs.shortline_refresh()
        assert out["score"] == 62 and out["level"] == "warm"
        assert out["trade_date"] == "2026-09-21"
        assert out["limit_up"] == 88 and out["limit_down"] == 3 and out["broken"] == 20
        assert out["stale"] is False and out["degraded"] == 0 and out["error"] is None
        assert "情绪 62 分 / warm" in out["summary"]
        assert "口径日 2026-09-21" in out["summary"]

    def test_stale_fallback_mentioned_in_summary(self, monkeypatch):
        from server import scan_jobs
        self._stub(monkeypatch, {
            "stale": True, "stale_date": "2026-09-18",
            "summary": {"trade_date": "2026-09-18", "score": 21, "level": "ice",
                        "limit_up_count": 0, "limit_down_count": 0, "broken_count": 0},
            "degraded": ["ladder", "limit_up"],
        })
        out = scan_jobs.shortline_refresh()
        assert out["stale"] is True and out["degraded"] == 2
        assert "回退历史快照 2026-09-18" in out["summary"]
        assert "降级 2 项" in out["summary"]

    def test_error_shell_does_not_crash(self, monkeypatch):
        from server import scan_jobs
        self._stub(monkeypatch, {
            "summary": None, "error": "上游数据源不可达，且无历史快照可回退",
            "degraded": ["ladder", "limit_up", "broken", "limit_down"],
        })
        out = scan_jobs.shortline_refresh()
        assert out["score"] is None and out["trade_date"] == "—"
        assert "上游数据源不可达" in out["summary"]

    def test_build_overview_exception_propagates_to_framework(self, monkeypatch):
        from server import shortline_service as sl
        from server import scan_jobs

        def _boom(*a, **k):
            raise RuntimeError("shortline.db 打不开")

        monkeypatch.setattr(sl, "build_overview", _boom)
        with pytest.raises(RuntimeError):
            scan_jobs.shortline_refresh()


class TestDefaultJobs:
    def test_keys_order_and_stagger(self, scans_on):
        from server.scan_jobs import default_jobs
        jobs = default_jobs()
        assert [j.key for j in jobs] == ["storage_scan", "thesis_refresh",
                                         "crowding_scan", "shortline_refresh",
                                         "kline_keepalive"]
        assert [j.name for j in jobs] == ["存储监控扫描", "研究台账刷新",
                                          "全市场拥挤度扫描", "短线情绪预热",
                                          "K 线保活"]
        assert all(j.enabled for j in jobs)
        assert all(j.interval_minutes > 0 and j.first_delay_s >= 0 for j in jobs)
        # 首次延迟必须错峰：启动期三个 warmup 正在抢 GIL，一起开跑会拖慢首字节
        delays = [j.first_delay_s for j in jobs]
        assert len(set(delays)) == len(delays)
        assert delays == sorted(delays)
        assert all(j.description for j in jobs)

    def test_master_switch_disables_everything(self, scans_on, monkeypatch):
        from server.scan_jobs import default_jobs
        monkeypatch.setattr(scans_on, "BACKGROUND_SCANS_ENABLED", False)
        assert all(not j.enabled for j in default_jobs())

    def test_individual_switches(self, scans_on, monkeypatch):
        from server.scan_jobs import default_jobs
        monkeypatch.setattr(scans_on, "SCAN_CROWDING_ENABLED", False)
        by = {j.key: j for j in default_jobs()}
        assert by["crowding_scan"].enabled is False
        assert by["storage_scan"].enabled is True

    def test_intervals_come_from_config(self, scans_on, monkeypatch):
        from server.scan_jobs import default_jobs
        monkeypatch.setattr(scans_on, "SCAN_THESIS_INTERVAL_MINUTES", 45)
        monkeypatch.setattr(scans_on, "SCAN_THESIS_FIRST_DELAY_S", 11)
        by = {j.key: j for j in default_jobs()}
        assert by["thesis_refresh"].interval_minutes == 45
        assert by["thesis_refresh"].first_delay_s == 11

    def test_register_default_jobs(self, scans_on):
        from server.background_scans import ScanManager
        from server.scan_jobs import register_default_jobs
        mgr = ScanManager()
        keys = register_default_jobs(mgr)
        assert keys == ["storage_scan", "thesis_refresh", "crowding_scan",
                        "shortline_refresh", "kline_keepalive"]
        assert mgr.keys() == keys

    def test_job_functions_are_the_real_ones(self, scans_on):
        from server import scan_jobs
        by = {j.key: j for j in scan_jobs.default_jobs()}
        assert by["storage_scan"].fn is scan_jobs.storage_scan
        assert by["thesis_refresh"].fn is scan_jobs.thesis_refresh
        assert by["crowding_scan"].fn is scan_jobs.crowding_scan
        assert by["shortline_refresh"].fn is scan_jobs.shortline_refresh
        assert by["kline_keepalive"].fn is scan_jobs.kline_keepalive

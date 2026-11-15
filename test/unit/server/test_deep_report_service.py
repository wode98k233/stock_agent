"""长报告中断清理单测：只收敛「上个进程遗留」的活跃报告。"""
from __future__ import annotations

from datetime import datetime

import pytest

from agents.deep_report.db import DeepReportDB
from agents.deep_report.models import ReportMeta
from server import deep_report_service as svc


def _meta(rid: str, status: str, updated_at: str,
          created_at: str = "2026-10-30 08:00:00") -> ReportMeta:
    return ReportMeta(
        rid=rid, universe="银行板块", user_request="", template_id="deep_sector_finance",
        variant="finance", status=status, created_at=created_at, updated_at=updated_at,
    )


@pytest.fixture()
def db(tmp_path):
    return DeepReportDB(str(tmp_path / "deep_report.db"))


PROCESS_START = datetime(2026, 11, 1, 12, 0, 0)
NOW = datetime(2026, 11, 1, 13, 0, 0)


class TestCleanupInterrupted:
    def test_marks_only_stale_active_reports(self, db):
        db.upsert_report(_meta("old-writing", "writing", "2026-10-30 09:00:00").to_dict())
        db.upsert_report(_meta("old-outlining", "outlining", "2026-10-30 09:00:00").to_dict())
        db.upsert_report(_meta("old-fetching", "fetching", "2026-10-30 09:00:00").to_dict())
        db.upsert_report(_meta("old-done", "done", "2026-10-30 09:00:00").to_dict())
        db.upsert_report(_meta("fresh-writing", "writing", "2026-11-01 12:30:00").to_dict())

        cleaned = svc.cleanup_interrupted_reports(
            db=db, process_started_at=PROCESS_START, now=NOW)

        assert cleaned == 3
        for rid in ("old-writing", "old-outlining", "old-fetching"):
            row = db.get_report(rid)
            assert row["status"] == "partial"
            assert row["interrupted_at"] == "2026-11-01 13:00:00"
        # 终态 / 本进程启动后的记录都不许动
        assert db.get_report("old-done")["status"] == "done"
        assert db.get_report("old-done")["interrupted_at"] is None
        assert db.get_report("fresh-writing")["status"] == "writing"

    def test_idempotent_second_run_is_noop(self, db):
        db.upsert_report(_meta("dr1", "writing", "2026-10-30 09:00:00").to_dict())
        assert svc.cleanup_interrupted_reports(
            db=db, process_started_at=PROCESS_START, now=NOW) == 1
        first = db.get_report("dr1")["interrupted_at"]

        assert svc.cleanup_interrupted_reports(
            db=db, process_started_at=PROCESS_START,
            now=datetime(2026, 11, 1, 14, 0, 0)) == 0
        assert db.get_report("dr1")["interrupted_at"] == first

    def test_missing_updated_at_falls_back_to_created_at(self, db):
        meta = _meta("dr2", "writing", "").to_dict()
        meta["updated_at"] = ""
        db.upsert_report(meta)
        # upsert_report 会用 _now() 兜底空 updated_at，这里直接改回空串模拟脏数据
        with db._ctx() as conn:
            conn.execute("UPDATE deep_report SET updated_at='', created_at='2026-10-01 08:00:00' "
                         "WHERE rid='dr2'")
        assert svc.cleanup_interrupted_reports(
            db=db, process_started_at=PROCESS_START, now=NOW) == 1
        assert db.get_report("dr2")["status"] == "partial"

    def test_once_guard_runs_only_once(self, db, monkeypatch):
        monkeypatch.setattr(svc, "_CLEANUP_DONE", False)
        db.upsert_report(_meta("dr3", "writing", "2026-10-30 09:00:00").to_dict())

        assert svc.cleanup_interrupted_reports_once(
            db=db, process_started_at=PROCESS_START, now=NOW) == 1
        db.upsert_report(_meta("dr4", "writing", "2026-10-30 09:00:00").to_dict())
        assert svc.cleanup_interrupted_reports_once(
            db=db, process_started_at=PROCESS_START, now=NOW) == 0
        assert db.get_report("dr4")["status"] == "writing"   # 第二次调用没扫

    def test_db_error_does_not_raise(self, monkeypatch):
        class Boom:
            _ctx = None

        assert svc.cleanup_interrupted_reports(
            db=Boom(), process_started_at=PROCESS_START, now=NOW) == 0

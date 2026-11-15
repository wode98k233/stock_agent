"""M3a 单测：deep_report.db（WAL/幂等/外键/事件游标）+ 文件↔DB 镜像。"""
from __future__ import annotations

import sqlite3

from agents.deep_report.db import DeepReportDB
from agents.deep_report.models import ReportMeta, SectionResult
from agents.deep_report.store import DeepReportStore


def _meta(rid="dr1"):
    return ReportMeta(rid=rid, universe="银行板块", user_request="", template_id="deep_sector_finance",
                      variant="finance", status="draft", created_at="2026-10-31 10:00:00")


class TestDB:
    def test_upsert_and_list(self, tmp_path):
        db = DeepReportDB(str(tmp_path / "dr.db"))
        db.upsert_report(_meta().to_dict())
        db.upsert_report({**_meta().to_dict(), "status": "done", "words": 1234})
        row = db.get_report("dr1")
        assert row["status"] == "done" and row["words"] == 1234
        assert [r["rid"] for r in db.list_reports()] == ["dr1"]

    def test_wal_enabled(self, tmp_path):
        db = DeepReportDB(str(tmp_path / "dr.db"))
        db.upsert_report(_meta().to_dict())
        conn = sqlite3.connect(str(tmp_path / "dr.db"))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert str(mode).lower() == "wal"

    def test_event_cursor(self, tmp_path):
        db = DeepReportDB(str(tmp_path / "dr.db"))
        db.upsert_report(_meta().to_dict())
        for i in range(5):
            db.append_event("dr1", "progress", {"i": i})
        first = db.events_since("dr1", 0)
        assert len(first) == 5 and first[0]["id"] < first[-1]["id"]
        tail = db.events_since("dr1", first[2]["id"])
        assert [e["data"]["i"] for e in tail] == [3, 4]

    def test_fk_cascade(self, tmp_path):
        db = DeepReportDB(str(tmp_path / "dr.db"))
        db.upsert_report(_meta().to_dict())
        db.upsert_section("dr1", {"seq": 1, "section_id": "summary", "title": "摘要", "status": "ok"})
        db.append_event("dr1", "phase", {"stage": "x"})
        with db._ctx() as conn:
            conn.execute("DELETE FROM deep_report WHERE rid=?", ("dr1",))
            left = conn.execute("SELECT COUNT(*) FROM deep_report_section").fetchone()[0]
            ev = conn.execute("SELECT COUNT(*) FROM deep_report_event").fetchone()[0]
        assert left == 0 and ev == 0

    def test_upsert_section_idempotent(self, tmp_path):
        db = DeepReportDB(str(tmp_path / "dr.db"))
        db.upsert_report(_meta().to_dict())
        db.upsert_section("dr1", {"seq": 1, "section_id": "s", "title": "t", "status": "writing"})
        db.upsert_section("dr1", {"seq": 1, "section_id": "s", "title": "t", "status": "ok",
                                  "actual_words": 800})
        with db._ctx() as conn:
            rows = conn.execute("SELECT status,actual_words FROM deep_report_section").fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["actual_words"] == 800


class TestStoreMirror:
    def test_store_mirrors_to_db(self, tmp_path):
        db = DeepReportDB(str(tmp_path / "dr.db"))
        store = DeepReportStore(tmp_path / "reports", db=db)
        meta = _meta("dr2")
        store.create(meta)
        store.save_section("dr2", SectionResult(1, "summary", "摘要", "ok", "## 1. 摘要\n正文", 10))
        store.save_evidence("dr2", {"ev_id": "ev_001", "slot": "valuation", "status": "ok",
                                    "asof": "2026-10-30", "items": [{"metric": "PB", "value": 1}],
                                    "gaps": []})
        store.append_event("dr2", "phase", {"stage": "writing"})
        store.finish("dr2", "done", words=10)

        assert db.get_report("dr2")["status"] == "done"
        assert len(db.events_since("dr2", 0)) == 1
        with db._ctx() as conn:
            assert conn.execute("SELECT COUNT(*) FROM deep_report_section").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM deep_report_evidence").fetchone()[0] == 1

    def test_broken_db_does_not_break_store(self, tmp_path):
        class Boom:
            def upsert_report(self, *a, **k):
                raise RuntimeError("db down")

        store = DeepReportStore(tmp_path / "reports2", db=Boom())
        store.create(_meta("dr3"))   # 不抛
        assert (store.dir_of("dr3") / "report.json").exists()


    def test_evidence_pk_is_per_report(self, tmp_path):
        """评审 P1-1：两份报告的 ev_001 必须各存一行（旧主键会互相覆盖）。"""
        db = DeepReportDB(str(tmp_path / "dr.db"))
        db.upsert_report(_meta("drA").to_dict())
        db.upsert_report(_meta("drB").to_dict())
        db.upsert_evidence("drA", {"ev_id": "ev_001", "slot": "valuation", "status": "ok"})
        db.upsert_evidence("drB", {"ev_id": "ev_001", "slot": "valuation", "status": "ok"})
        conn = sqlite3.connect(str(tmp_path / "dr.db"))
        try:
            rows = conn.execute(
                "SELECT rid, ev_id FROM deep_report_evidence ORDER BY rid").fetchall()
        finally:
            conn.close()
        assert rows == [("drA", "ev_001"), ("drB", "ev_001")]


    def test_prepare_runs_once_but_fk_each_connection(self, tmp_path, monkeypatch):
        import utils.db_schema.migrator as mig
        from agents.deep_report import db as db_mod
        db_mod.reset_prepare_cache()
        calls = {"n": 0}
        orig = mig.ensure_schema

        def counted(*a, **kw):
            calls["n"] += 1
            return orig(*a, **kw)

        monkeypatch.setattr(mig, "ensure_schema", counted)
        path = str(tmp_path / "dr_once.db")
        d = DeepReportDB(path)
        d.upsert_report(_meta().to_dict())
        d.upsert_report({**_meta().to_dict(), "status": "done"})
        assert calls["n"] == 1, "同一路径只该建表一次"
        with d._ctx() as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

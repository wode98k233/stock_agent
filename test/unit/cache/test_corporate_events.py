"""corporate_events 表读写（不联网、不碰真实库）。"""
import sqlite3

from utils.cache import market_data_db as mdb


def _ctx(tmp_path):
    db = tmp_path / "market.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE corporate_events (
        event_id TEXT PRIMARY KEY, code TEXT NOT NULL, event_type TEXT NOT NULL,
        event_date TEXT NOT NULL, title TEXT NOT NULL, payload TEXT,
        source TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
    con.commit()
    con.close()

    class _C:
        def __enter__(self):
            self.c = sqlite3.connect(db)
            self.c.row_factory = sqlite3.Row
            return self.c

        def __exit__(self, *exc):
            self.c.commit()
            self.c.close()

    return db, _C


def _rec(eid, code, etype, edate, title="T", payload=None):
    return {"event_id": eid, "code": code, "event_type": etype,
            "event_date": edate, "title": title, "payload": payload, "source": "test"}


def test_upsert_is_idempotent(tmp_path, monkeypatch):
    db, ctx = _ctx(tmp_path)
    monkeypatch.setattr(mdb, "get_market_data_db", ctx)
    rec = _rec("e1", "600519", "unlock", "2026-11-09", payload={"qty": 100})
    assert mdb.upsert_corporate_events([rec]) == 1
    assert mdb.upsert_corporate_events([rec]) == 1
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from corporate_events").fetchone()[0] == 1
    con.close()


def test_query_filters_and_decodes_payload(tmp_path, monkeypatch):
    db, ctx = _ctx(tmp_path)
    monkeypatch.setattr(mdb, "get_market_data_db", ctx)
    mdb.upsert_corporate_events([
        _rec("a", "600519", "unlock", "2026-11-09", payload={"qty": 100}),
        _rec("b", "600519", "dividend", "2026-11-20"),
        _rec("c", "300750", "unlock", "2026-11-10"),
    ])
    rows = mdb.query_corporate_events(codes=["600519"], date_from="2026-11-01",
                                      date_to="2026-11-30", types=["unlock"])
    assert [r["event_id"] for r in rows] == ["a"]
    assert rows[0]["payload"] == {"qty": 100}

    all_rows = mdb.query_corporate_events(date_from="2026-11-01", date_to="2026-11-30")
    assert [r["event_id"] for r in all_rows] == ["a", "c", "b"]   # 日期升序
    assert mdb.query_corporate_events(date_from="2026-12-01") == []
    assert len(mdb.query_corporate_events(date_from="2026-11-01", limit=2)) == 2

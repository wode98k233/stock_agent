"""store：reviews 表 CRUD 与按 target 的版本链查询。"""
import sqlite3

import pytest

from utils.db_schema.registry import ALL_DBS
from utils.db_schema.migrator import ensure_schema
from eval.review import store


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    c.row_factory = sqlite3.Row
    d = ALL_DBS["agent_trace"]
    ensure_schema(c, d.tables, d.special_migrations)
    yield c
    c.close()


def test_create_and_finish_review(conn):
    uuid = store.create_review(conn, "run", "r1", "v1", "judge-model")
    assert uuid
    row = store.get_review(conn, uuid)
    assert row["status"] == "running"

    store.finish_review(conn, uuid, overall=88.5, scores={"tool_usage": 88.5},
                        findings=[{"step_ref": 3}], inputs={"run_id": "r1"}, duration_ms=1234.0)
    row = store.get_review(conn, uuid)
    assert row["status"] == "done"
    assert row["overall"] == 88.5
    assert row["scores"] == {"tool_usage": 88.5}
    assert row["findings"] == [{"step_ref": 3}]
    assert row["inputs"] == {"run_id": "r1"}
    assert row["duration_ms"] == 1234.0


def test_fail_review(conn):
    uuid = store.create_review(conn, "run", "r1", "v1", "m")
    store.fail_review(conn, uuid, "boom")
    row = store.get_review(conn, uuid)
    assert row["status"] == "failed"
    assert row["error"] == "boom"


def test_list_reviews_version_chain(conn):
    for i in range(3):
        uuid = store.create_review(conn, "run", "r1", "v1", "m")
        store.finish_review(conn, uuid, overall=float(i), scores={}, findings=[])
    items = store.list_reviews(conn, "run", "r1")
    assert len(items) == 3
    assert items[0]["created_at"] >= items[-1]["created_at"]


def test_list_reviews_isolated_by_target(conn):
    store.create_review(conn, "run", "r1", "v1", "m")
    store.create_review(conn, "run", "r2", "v1", "m")
    assert len(store.list_reviews(conn, "run", "r1")) == 1


def test_list_reviews_limit(conn):
    for i in range(3):
        uuid = store.create_review(conn, "run", "r1", "v1", "m")
        store.finish_review(conn, uuid, overall=float(i), scores={}, findings=[])
    items = store.list_reviews(conn, "run", "r1", limit=1)
    assert len(items) == 1
    assert items[0]["overall"] == 2.0
    assert store.list_reviews(conn, "run", "r1", limit=-1) == []


def test_list_reviews_same_created_at_falls_back_to_id_desc(conn):
    for i in range(3):
        uuid = store.create_review(conn, "run", "r1", "v1", "m")
        store.finish_review(conn, uuid, overall=float(i), scores={}, findings=[])
    conn.execute("UPDATE reviews SET created_at='2026-09-18T00:00:00+00:00'")
    conn.commit()
    items = store.list_reviews(conn, "run", "r1")
    assert [item["overall"] for item in items] == [2.0, 1.0, 0.0]


def test_corrupted_json_columns_read_back_raw(conn):
    uuid = store.create_review(conn, "run", "r1", "v1", "m")
    store.finish_review(conn, uuid, overall=1.0, scores={}, findings=[])
    conn.execute(
        "UPDATE reviews SET scores_json='not-json', findings_json='[broken', "
        "inputs_json='nope' WHERE review_uuid=?",
        (uuid,),
    )
    conn.commit()
    row = store.get_review(conn, uuid)
    assert row["scores"] == "not-json"
    assert row["findings"] == "[broken"
    assert row["inputs"] == "nope"


def test_finish_review_inputs_semantics(conn):
    empty = store.create_review(conn, "run", "r1", "v1", "m")
    store.finish_review(conn, empty, overall=1.0, scores={}, findings=[], inputs={})
    assert store.get_review(conn, empty)["inputs"] == {}

    missing = store.create_review(conn, "run", "r2", "v1", "m")
    store.finish_review(conn, missing, overall=2.0, scores={}, findings=[])
    assert store.get_review(conn, missing)["inputs"] is None


def test_write_operations_unknown_uuid(conn):
    assert store.get_review(conn, "no-such-uuid") is None
    assert store.finish_review(conn, "no-such-uuid", overall=1.0,
                               scores={}, findings=[]) is False
    assert store.fail_review(conn, "no-such-uuid", "boom") is False


def test_finish_review_after_done_is_rejected(conn):
    uuid = store.create_review(conn, "run", "r1", "v1", "m")
    assert store.finish_review(conn, uuid, overall=80.0, scores={}, findings=[]) is True
    assert store.finish_review(conn, uuid, overall=10.0, scores={}, findings=[]) is False
    row = store.get_review(conn, uuid)
    assert row["status"] == "done"
    assert row["overall"] == 80.0


def test_fail_review_after_done_is_rejected(conn):
    uuid = store.create_review(conn, "run", "r1", "v1", "m")
    assert store.finish_review(conn, uuid, overall=80.0, scores={}, findings=[]) is True
    assert store.fail_review(conn, uuid, "zombie worker") is False
    row = store.get_review(conn, uuid)
    assert row["status"] == "done"
    assert row["error"] is None


def test_fail_review_truncates_error(conn):
    uuid = store.create_review(conn, "run", "r1", "v1", "m")
    assert store.fail_review(conn, uuid, "x" * 2500) is True
    row = store.get_review(conn, uuid)
    assert row["error"] == "x" * 2000
    assert len(row["error"]) == 2000

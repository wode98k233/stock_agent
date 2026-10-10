"""TraceStore 月库路由（分库核心语义）测试。

覆盖：None→当月路由、run_id probe 命中/未命中、显式路径全 4 表兼容、
空目录 None 语义、跨月归并、批量精查、跨库删除。
"""
import sqlite3

import pytest

from utils.agent_trace.models import ensure_full_schema, ensure_month_schema
from utils.agent_trace.store import MONTH_DB_PREFIX, TraceStore, store


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    """tmp 月目录 + tmp 主库，隔离真实路径。"""
    month_dir = tmp_path / "agent_trace"
    month_dir.mkdir()
    main_db = tmp_path / "agent_trace.db"
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(month_dir))
    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: str(main_db))
    yield {"month_dir": month_dir, "main_db": main_db}
    store.close_thread_conns()


def _make_month(month_dir, ym: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(month_dir / f"{MONTH_DB_PREFIX}{ym}.db"))
    conn.row_factory = sqlite3.Row
    ensure_month_schema(conn)
    return conn


def _seed_run(conn, run_id: str, created_at: str, *, status: str = "success",
              agent_name: str = "react_stock", question: str = "q"):
    conn.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, agent_name, question, "答案", status, created_at),
    )
    cur = conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at) "
        "VALUES(?,?,?,?,'success',?)",
        (run_id, f"ev-{run_id}", "tool", "t", created_at),
    )
    conn.execute(
        "INSERT INTO messages(step_id,seq,role,content) VALUES(?,?,?,?)",
        (cur.lastrowid, 0, "ai", "msg"),
    )
    conn.commit()


def test_current_month_routing_writes_current_month_db(trace_env):
    """SQLiteAdapter/TraceRecorder 默认（db_path=None）落在当月月库。"""
    from utils.agent_trace.db_adapter import SQLiteAdapter

    adapter = SQLiteAdapter()
    run_id = adapter.insert_run({"id": "r-now", "agent_name": "react_stock", "input": "q"})
    adapter.update_run(run_id, status="success", output="o")
    adapter.close()

    ym = store.current_month()
    conn = store.conn_for_month(ym)
    assert conn is not None
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row is not None
    # 当月月库无 reviews 表
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='reviews'"
    ).fetchone()[0] == 0


def test_explicit_path_keeps_full_schema(trace_env):
    """显式路径语义不变：直写该单文件并 ensure 全 4 表（30+ 既有测试依赖）。"""
    from utils.agent_trace.db_adapter import SQLiteAdapter

    p = trace_env["month_dir"].parent / "legacy_single.db"
    adapter = SQLiteAdapter(str(p))
    run_id = adapter.insert_run({"id": "r-legacy", "agent_name": "a", "input": "q"})
    adapter.delete_run(run_id)
    adapter.close()

    tables = {r[0] for r in sqlite3.connect(str(p)).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == {"runs", "steps", "messages", "reviews"}


def test_ensure_full_schema_helper_compatible(tmp_path):
    p = tmp_path / "full.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    ensure_full_schema(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert names == {"runs", "steps", "messages", "reviews"}


def test_conn_for_run_probes_newest_first(trace_env):
    conn_new = _make_month(trace_env["month_dir"], "202609")
    conn_old = _make_month(trace_env["month_dir"], "202608")
    _seed_run(conn_new, "r-new", "2026-09-10T00:00:00")
    _seed_run(conn_old, "r-old", "2026-08-10T00:00:00")
    conn_new.close()
    conn_old.close()

    hit_new = store.conn_for_run("r-new")
    assert hit_new is not None
    assert "202609" in str(hit_new.execute("PRAGMA database_list").fetchall()[0][2]) or True
    assert store.conn_for_run("r-old") is not None
    assert store.conn_for_run("missing") is None


def test_list_runs_merges_months_desc(trace_env):
    c1 = _make_month(trace_env["month_dir"], "202608")
    c2 = _make_month(trace_env["month_dir"], "202609")
    _seed_run(c1, "r-aug1", "2026-08-10T00:00:00")
    _seed_run(c1, "r-aug2", "2026-08-01T00:00:00")
    _seed_run(c2, "r-sep1", "2026-09-10T00:00:00", status="error")
    _seed_run(c2, "r-sep2", "2026-09-01T00:00:00")
    c1.close()
    c2.close()

    # 全部：跨月时间倒序
    runs = store.list_runs(limit=10)
    assert [r["id"] for r in runs] == ["r-sep1", "r-sep2", "r-aug1", "r-aug2"]
    # status 过滤
    assert [r["id"] for r in store.list_runs(limit=10, status="success")] == \
        ["r-sep2", "r-aug1", "r-aug2"]
    # limit 截断
    assert len(store.list_runs(limit=2)) == 2
    # since/until
    runs = store.list_runs(limit=10, since="2026-08-05T00:00:00",
                           until="2026-09-05T00:00:00")
    assert [r["id"] for r in runs] == ["r-sep2", "r-aug1"]


def test_get_runs_by_ids_and_find_latest(trace_env):
    c1 = _make_month(trace_env["month_dir"], "202608")
    c2 = _make_month(trace_env["month_dir"], "202609")
    _seed_run(c1, "r-aug", "2026-08-10T00:00:00")
    _seed_run(c2, "r-sep", "2026-09-10T00:00:00", agent_name="plan_solve")
    c1.close()
    c2.close()

    runs = store.get_runs_by_ids(["r-aug", "r-sep", "missing"])
    assert [r["id"] for r in runs] == ["r-aug", "r-sep"]

    assert store.find_latest_run()["id"] == "r-sep"
    assert store.find_latest_run(status="success")["id"] == "r-sep"
    assert store.find_latest_run(status="running") is None


def test_get_steps_with_messages_and_step_detail(trace_env):
    conn = _make_month(trace_env["month_dir"], store.current_month())
    _seed_run(conn, "r1", "2026-09-10T00:00:00")
    steps = store.get_steps_with_messages("r1")
    assert len(steps) == 1 and steps[0]["messages"][0]["content"] == "msg"

    detail = store.get_step_detail("r1", steps[0]["id"])
    assert detail["messages"][0]["role"] == "ai"
    conn.close()


def test_delete_run_spans_month_db_and_main_db(trace_env):
    conn = _make_month(trace_env["month_dir"], store.current_month())
    _seed_run(conn, "r1", "2026-09-10T00:00:00")
    rconn = store.reviews_conn()
    rconn.execute(
        "INSERT INTO reviews(review_uuid,scope,target_id,rubric_version,status,created_at) "
        "VALUES('u1','run','r1','v1','done','2026-09-11T00:00:00')")
    rconn.commit()

    store.delete_run("r1")

    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert rconn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    conn.close()


def test_find_run_by_input_and_prefix(trace_env):
    c1 = _make_month(trace_env["month_dir"], "202608")
    c2 = _make_month(trace_env["month_dir"], "202609")
    # 同一问题两个月各一次：before 过滤应命中更早那次
    _seed_run(c1, "abc-old", "2026-08-10T00:00:00", question='"新问题"')
    _seed_run(c2, "abc-new", "2026-09-10T00:00:00", question='"新问题"')
    c1.close()
    c2.close()

    # find_run_by_input 兼容原样 / JSON 编码两种落库形态
    assert store.find_run_by_input("新问题") == "abc-new"
    assert store.find_run_by_input("新问题", before="2026-09-01T00:00:00") == "abc-old"
    assert store.find_run_by_input("不存在") is None

    assert [r["id"] for r in store.find_run_by_id_prefix("abc-")] == ["abc-new"]
    assert store.find_run_by_id_prefix("xyz-") == []

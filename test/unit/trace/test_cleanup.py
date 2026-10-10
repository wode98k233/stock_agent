"""trace 月库 cleanup：preview 不动文件 / apply 级联删主库 reviews / keep-months 边界。"""
import sqlite3

import pytest

from utils.agent_trace import cleanup as trace_cleanup
from utils.agent_trace.models import ensure_month_schema, ensure_reviews_schema
from utils.agent_trace.store import MONTH_DB_PREFIX, TraceStore, store


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    month_dir = tmp_path / "agent_trace"
    month_dir.mkdir()
    main_db = tmp_path / "agent_trace.db"
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(month_dir))
    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: str(main_db))
    yield {"month_dir": month_dir, "main_db": main_db}
    store.close_thread_conns()


def _make_month(month_dir, ym: str, n_runs: int = 1) -> sqlite3.Connection:
    conn = sqlite3.connect(str(month_dir / f"{MONTH_DB_PREFIX}{ym}.db"))
    conn.row_factory = sqlite3.Row
    ensure_month_schema(conn)
    for i in range(n_runs):
        rid = f"{ym}-{i}"
        conn.execute(
            "INSERT INTO runs(id,agent_name,input,status,created_at) "
            f"VALUES('{rid}','a','q','success','{ym[:4]}-{ym[4:]}-10T00:00:00')")
    conn.commit()
    return conn


def _seed_review(main_db, uuid: str, target_id: str):
    conn = sqlite3.connect(str(main_db))
    conn.row_factory = sqlite3.Row
    ensure_reviews_schema(conn)
    conn.execute(
        "INSERT INTO reviews(review_uuid,scope,target_id,rubric_version,status,created_at) "
        "VALUES(?,?,?,?,?,?)", (uuid, "run", target_id, "v1", "done", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _ym(base: str, delta: int) -> str:
    """YYYYMM 月份平移（负数往前）—— 测试数据相对**当前月**构造，杜绝跨月失效。

    根因：原先硬编码 202601/202603/202609 并注释「当月是 2026-09」，
    `preview` 以 `store.current_month()`（真实 UTC 当前月）为锚，跨月即失败
    （2026-10-01 实测 test_cleanup_keep_months_boundary 开始报红）。
    """
    idx = trace_cleanup._month_index(base) + delta
    y, m0 = divmod(idx - 1, 12)
    return f"{y:04d}{m0 + 1:02d}"


def test_cleanup_preview_does_not_write(trace_env):
    cur = trace_cleanup.store.current_month()
    old, inside = _ym(cur, -6), _ym(cur, -1)      # keep_months=6 → 窗口 [cur-5, cur]
    c1 = _make_month(trace_env["month_dir"], old, n_runs=2)
    c2 = _make_month(trace_env["month_dir"], inside)
    c1.close()
    c2.close()

    plan = trace_cleanup.preview(keep_months=6)
    assert [m.month for m in plan.months] == [old]
    assert plan.months[0].runs == 2
    assert plan.months[0].size_mb >= 0
    # 文件仍在
    assert (trace_env["month_dir"] / f"{MONTH_DB_PREFIX}{old}.db").exists()


def test_cleanup_apply_deletes_files_and_cascades_reviews(trace_env):
    cur = trace_cleanup.store.current_month()
    old, inside = _ym(cur, -6), _ym(cur, -1)
    c1 = _make_month(trace_env["month_dir"], old)
    c2 = _make_month(trace_env["month_dir"], inside)
    c1.close()
    c2.close()
    _seed_review(trace_env["main_db"], "u-old", f"{old}-0")
    _seed_review(trace_env["main_db"], "u-keep", f"{inside}-0")

    plan = trace_cleanup.preview(keep_months=6)
    result = trace_cleanup.apply(plan)

    assert result["removed_months"] == [old]
    assert result["reviews_deleted"] == 1
    assert not (trace_env["month_dir"] / f"{MONTH_DB_PREFIX}{old}.db").exists()
    assert (trace_env["month_dir"] / f"{MONTH_DB_PREFIX}{inside}.db").exists()
    # 主库 reviews 只剩保留月的那条
    conn = sqlite3.connect(str(trace_env["main_db"]))
    n = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    conn.close()
    assert n == 1


def test_cleanup_keep_months_boundary(trace_env):
    """恰好等于保留月数时不过期（以当前月为锚，日期无关）。"""
    keep = 9
    cur = trace_cleanup.store.current_month()
    oldest_kept, older = _ym(cur, -(keep - 1)), _ym(cur, -keep)
    for ym in (older, oldest_kept, cur):
        _make_month(trace_env["month_dir"], ym).close()
    plan = trace_cleanup.preview(keep_months=keep)
    assert [m.month for m in plan.months] == [older]
    result = trace_cleanup.apply(plan)
    assert result["removed_months"] == [older]


def test_cleanup_keep_months_zero_clears_all(trace_env):
    cur = trace_cleanup.store.current_month()
    _make_month(trace_env["month_dir"], cur).close()
    plan = trace_cleanup.preview(keep_months=0)
    assert [m.month for m in plan.months] == [cur]
    result = trace_cleanup.apply(plan)
    assert result["removed_months"] == [cur]

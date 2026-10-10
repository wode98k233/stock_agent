"""trace 连接线程隔离与生命周期回归（2026-09 分库改造后，针对 store.reviews_conn）。

背景：reviews 主库连接经 `utils.agent_trace.store` 的线程局部缓存管理
（每线程独立连接 + LRU），并发线程不得共享同一连接。
"""
import threading

from utils.agent_trace.store import TraceStore, store


def _use_tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "agent_trace.db"
    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: str(path))
    return path


def test_same_thread_reuses_connection(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    try:
        assert store.reviews_conn() is store.reviews_conn()
    finally:
        store.close_thread_conns()


def test_threads_do_not_share_connection(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    main_conn = store.reviews_conn()
    holder = {}

    def worker():
        conn = store.reviews_conn()
        holder["is_main"] = conn is main_conn
        holder["usable"] = conn.execute("SELECT 1 AS c").fetchone()["c"]

    t = threading.Thread(target=worker, name="trace-poller")
    t.start()
    t.join(timeout=10)

    try:
        assert holder["is_main"] is False
        assert holder["usable"] == 1
    finally:
        store.close_thread_conns()


def test_row_factory_and_pragmas_applied(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    try:
        conn = store.reviews_conn()
        assert conn.row_factory is not None
        conn.execute("CREATE TABLE IF NOT EXISTS t (a)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        # 行工厂生效（按键取列）
        assert conn.execute("SELECT a FROM t").fetchone()["a"] == 1
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        store.close_thread_conns()


def test_close_then_reuse_rebuilds(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    try:
        first = store.reviews_conn()
        store.close_thread_conns()
        second = store.reviews_conn()
        assert second is not first
        assert second.execute("SELECT 1").fetchone()[0] == 1
    finally:
        store.close_thread_conns()


def test_conn_for_run_returns_none_without_month_dbs(tmp_path, monkeypatch):
    """无任何月库时 conn_for_run 返回 None（原 get_trace_conn 的 None 语义移到此处）。"""
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(tmp_path / "empty"))
    try:
        assert store.iter_existing_months() == []
        assert store.conn_for_run("whatever") is None
        assert store.list_runs(limit=5) == []
    finally:
        store.close_thread_conns()


def test_concurrent_readers_stay_clean(tmp_path, monkeypatch):
    """12 线程并发读：共享连接时同一连接上的游标会互相踩（'another row available'）。"""
    _use_tmp_db(tmp_path, monkeypatch)
    setup = store.reviews_conn()
    setup.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
    setup.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(200)])
    setup.commit()

    errors = []

    def worker():
        try:
            for _ in range(10):
                rows = store.reviews_conn().execute("SELECT a FROM t").fetchall()
                assert len(rows) == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    try:
        assert errors == [], f"并发读出现异常: {errors[:3]}"
    finally:
        store.close_thread_conns()

"""FTS5Backend 连接模型回归测试（2026-09-21 改为「一连接一线程」）。

背景：FTS5Backend 是单例，会被「事件循环线程 / asyncio worker 线程 / 记忆预热
线程 / 定时清理协程」同时使用。原先共用一个 `check_same_thread=False` 连接，
`check_same_thread=False` 只关掉 Python 层校验、**并不会让连接变线程安全**。
改为每线程独立连接（与 server/monitoring/store.py、signal_eval/db.py 一致）。
"""
import threading

from memory.backend.base import MemoryEntry
from memory.backend.fts5 import FTS5Backend


def _entry(name: str) -> MemoryEntry:
    return MemoryEntry(
        entry_id=f"t_{name}",
        stock_code="603369",
        stock_name="今世缘",
        content=f"{name} 测试内容",
        confidence=0.8,
    )


def test_same_thread_reuses_single_connection(tmp_path):
    be = FTS5Backend(str(tmp_path / "mem.db"))
    try:
        assert be._get_conn() is be._get_conn()
    finally:
        be.close()


def test_each_thread_gets_own_connection(tmp_path):
    be = FTS5Backend(str(tmp_path / "mem.db"))
    main_conn = be._get_conn()
    holder = {}

    def worker():
        holder["conn"] = be._get_conn()
        holder["same_as_main"] = holder["conn"] is main_conn

    t = threading.Thread(target=worker, name="fts5-worker")
    t.start()
    t.join(timeout=10)

    try:
        assert holder["same_as_main"] is False, "不同线程不应共用同一个连接"
        assert holder["conn"] is not None
    finally:
        be.close()


def test_concurrent_writers_do_not_share_connection(tmp_path):
    """8 线程并发写入：改造前共享连接会抛 'another row available' /
    'cannot commit - no transaction is active' 一类脏错误。"""
    be = FTS5Backend(str(tmp_path / "mem.db"))
    errors = []

    def worker(idx: int):
        try:
            for n in range(5):
                be.add(_entry(f"w{idx}_{n}"))
                be.search("测试")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    try:
        assert errors == [], f"并发访问出现异常: {errors[:3]}"
        assert be.count() == 40
    finally:
        be.close()


def test_close_then_reuse_rebuilds_connection(tmp_path):
    be = FTS5Backend(str(tmp_path / "mem.db"))
    first = be._get_conn()
    be.close()
    second = be._get_conn()
    try:
        assert second is not first
        assert be.count() >= 0
    finally:
        be.close()

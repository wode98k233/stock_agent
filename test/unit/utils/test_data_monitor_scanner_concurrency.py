"""`utils/data_monitor/scanner.py` 的并发纪律回归测试（2026-09-21 崩溃修复）。

线上实证（`logs/native_crash.log`，faulthandler 首个线程 = 崩溃线程）：

```
Windows fatal exception: access violation
Thread 0x00002bcc (most recent call first):
  File "...\\utils\\data_monitor\\scanner.py", line 81 in _table_stat
  File "...\\concurrent\\futures\\thread.py", line 58 in run
```

原实现：**一个只读连接被 ThreadPoolExecutor 的 4 个 worker 共用**，
且主线程 `shutdown(wait=False)` 后**立刻 `conn.close()`** →
在 worker 仍在取行的窗口里释放连接/语句，取行时要回调 `row_factory` → 空指针调用 →
原生访问违例（0xC0000005，dump 目标地址恒为 0x8）→ 进程静默消失。

本测试锁死修复后的两条不变量：
1. **任何连接都不跨线程使用**（谁创建谁使用谁关闭）；
2. 只读连接用**严格模式**（默认 `check_same_thread=True`）——被别的线程误用时
   抛可捕获的 `ProgrammingError`，而不是让解释器崩掉。
"""
import sqlite3
import threading

import pytest

from utils.data_monitor.discovery import Asset
from utils.data_monitor import scanner


def _make_db(path, tables: int = 6, rows: int = 200) -> None:
    conn = sqlite3.connect(str(path))
    for i in range(tables):
        conn.execute(f'CREATE TABLE t{i} (a INTEGER, b TEXT, updated_at TEXT)')
        conn.executemany(f'INSERT INTO t{i} (a, b, updated_at) VALUES (?, ?, ?)',
                         [(n, "x", "2026-09-21") for n in range(rows)])
    conn.commit()
    conn.close()


@pytest.fixture()
def db_asset(tmp_path):
    path = tmp_path / "sample.db"
    _make_db(path)
    return Asset(key="sample.db", category="db", name="sample", path=str(path))


def test_scan_db_collects_tables_and_counts(db_asset):
    stat = scanner._scan_db(db_asset, scanner.AssetStat(db_asset))
    assert stat.error is None
    assert [t["name"] for t in stat.tables] == [f"t{i}" for i in range(6)]
    assert all(t["count"] == 200 for t in stat.tables)
    assert all(t["latest"] == "2026-09-21" for t in stat.tables)
    assert stat.record_count == 6 * 200


def test_connections_never_cross_threads(db_asset, monkeypatch):
    """核心回归：每个连接只在一个线程里出现（旧实现是「1 个连接 × 4 个线程」）。"""
    created: list = []          # 存连接对象本身（持引用避免 id() 被复用）
    real_open = scanner._open_ro

    def spy(path):
        conn = real_open(path)
        created.append((conn, threading.get_ident()))
        return conn

    monkeypatch.setattr(scanner, "_open_ro", spy)
    stat = scanner._scan_db(db_asset, scanner.AssetStat(db_asset))

    assert not stat.error
    by_conn: dict[int, set[int]] = {}
    for conn, thread_id in created:
        by_conn.setdefault(id(conn), set()).add(thread_id)
    offenders = {cid: tids for cid, tids in by_conn.items() if len(tids) > 1}
    assert not offenders, f"存在跨线程共用的连接: {offenders}"

    # 连接数 = 1（列表清单） + 每表 1 条；没有复用/缓存
    assert len(created) == 1 + len(stat.tables)
    assert len(by_conn) == len(created)


def test_open_ro_is_thread_strict(db_asset):
    """严格模式：连接被别的线程使用时应抛 ProgrammingError，而不是崩溃。"""
    conn = scanner._open_ro(db_asset.path)
    holder = {}

    def use_from_other_thread():
        try:
            conn.execute("SELECT 1").fetchone()
            holder["result"] = "ok"
        except Exception as exc:  # noqa: BLE001
            holder["result"] = exc

    t = threading.Thread(target=use_from_other_thread)
    t.start()
    t.join(timeout=10)
    conn.close()

    assert isinstance(holder["result"], sqlite3.ProgrammingError), holder["result"]


def test_open_ro_is_read_only(db_asset):
    conn = scanner._open_ro(db_asset.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t0 (a) VALUES (1)")
    finally:
        conn.close()


def test_scan_db_skips_bad_table_without_failing(tmp_path):
    """单表异常只降级为 skipped，不影响整库统计。"""
    path = tmp_path / "broken.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE ok (a INTEGER)")
    conn.execute("INSERT INTO ok VALUES (1)")
    conn.execute("CREATE VIEW v AS SELECT * FROM ok")
    conn.commit()
    conn.close()
    asset = Asset(key="broken.db", category="db", name="broken", path=str(path))

    stat = scanner._scan_db(asset, scanner.AssetStat(asset))
    assert stat.error is None
    names = {t["name"] for t in stat.tables}
    assert "ok" in names  # 视图不在 sqlite_master 的 type='table' 里，不该出现
    assert "v" not in names


def test_scan_db_concurrent_with_other_scans(db_asset):
    """外层并发（scan() 的形状）：多个 _scan_db 同时跑也不能互相踩。"""
    errors = []

    def run():
        try:
            stat = scanner._scan_db(db_asset, scanner.AssetStat(db_asset))
            assert stat.record_count == 6 * 200
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []

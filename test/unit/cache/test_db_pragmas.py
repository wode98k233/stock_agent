"""SQLite 连接工厂的 PRAGMA / on_connect 钩子。

事故背景（2026-10-18 全站卡顿）：market_data.db 用的是 SQLite 默认的
rollback journal（delete）模式 —— 写事务持**排他锁**，期间所有读被阻塞
（每次最多等 busy_timeout=5000ms）。K 线保活写它 42 秒，web 全站接口就 5~12 秒。
同一个项目里 backtest.db 早就因为同样的理由开了 WAL，这个库当时漏了。
"""
import os
import sqlite3
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.cache.db_utils import make_db_context  # noqa: E402
from utils.cache import market_data_db as mdd  # noqa: E402


def _tmp_db():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE x(a)")
    c.commit()
    c.close()
    return p


class TestMakeDbContext:

    def test_pragmas_applied(self):
        p = _tmp_db()
        get = make_db_context(lambda: p, pragmas=("PRAGMA synchronous = NORMAL",))
        with get() as conn:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_bad_pragma_is_swallowed(self):
        """优化项失败不能让数据访问失败。"""
        p = _tmp_db()
        get = make_db_context(lambda: p, pragmas=("PRAGMA 这不是合法语句",))
        with get() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1

    def test_on_connect_called(self):
        p = _tmp_db()
        seen = []
        get = make_db_context(lambda: p, on_connect=lambda c: seen.append(1))
        with get() as conn:
            conn.execute("SELECT 1")
        assert seen == [1]

    def test_on_connect_exception_is_swallowed(self):
        p = _tmp_db()

        def boom(_c):
            raise RuntimeError("x")
        get = make_db_context(lambda: p, on_connect=boom)
        with get() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1


class TestEnsureWal:

    def test_switches_to_wal_and_persists(self, monkeypatch):
        p = _tmp_db()
        monkeypatch.setattr(mdd, "_wal_ensured", False)
        c = sqlite3.connect(p)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        mdd._ensure_wal(c)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        c.close()
        # 模式写在库文件头，重开仍然有效
        c2 = sqlite3.connect(p)
        assert c2.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        c2.close()

    def test_busy_db_does_not_raise(self, monkeypatch, caplog):
        """库被占用时切换会失败 —— 只能告警，绝不能让数据访问失败。"""
        p = _tmp_db()
        holder = sqlite3.connect(p)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO x VALUES(1)")
        monkeypatch.setattr(mdd, "_wal_ensured", False)
        c = sqlite3.connect(p, timeout=1)
        mdd._ensure_wal(c)          # 不得抛
        assert mdd._wal_ensured is False, "没切成就不该置位（下次连接再试）"
        holder.rollback()
        holder.close()

    def test_latched_after_success(self, monkeypatch):
        """成功后不再重复尝试。

        否则每次建连都会发 PRAGMA journal_mode = WAL，而那条语句在库忙时**要抢独占锁**，
        等于给每个请求加一次等待 —— 比不开 WAL 还慢。
        """
        p = _tmp_db()
        monkeypatch.setattr(mdd, "_wal_ensured", False)

        class _Spy(sqlite3.Connection):
            calls = []

            def execute(self, *a, **k):
                _Spy.calls.append(a[0] if a else "")
                return super().execute(*a, **k)

        _Spy.calls = []
        c = sqlite3.connect(p, factory=_Spy)
        mdd._ensure_wal(c)
        assert mdd._wal_ensured is True
        first = len(_Spy.calls)
        assert first > 0
        mdd._ensure_wal(c)          # 再调一次：必须直接返回，不再发任何语句
        assert len(_Spy.calls) == first, "已置位后不得再发 PRAGMA"
        c.close()

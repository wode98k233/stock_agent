"""`utils.sqlite_guard` 回归测试。

固化行为（对应 2026-09-21 崩溃排查结论）：
- `del conn.row_factory` / `del conn.text_factory` 会让 C 层槽位变 NULL，
  下一次查询以 NULL 为可调用对象调用 → 解释器原生崩溃（0xC0000005），
  Python 层连异常都拿不到（2026-09-21 已现场复现并定位）。
- 因此守卫版连接必须在 `__delattr__` 直接拒绝删除这两个属性，
  **且不误伤正常用法**（显式赋 None / str / sqlite3.Row 仍要可用）。
"""
import sqlite3

import pytest

from utils.sqlite_guard import FACTORY_ATTRS, GuardedConnection, connect


def test_connect_returns_usable_guarded_connection():
    conn = connect(":memory:")
    assert isinstance(conn, sqlite3.Connection)
    assert isinstance(conn, GuardedConnection)
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    assert conn.execute("SELECT a FROM t").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("attr", FACTORY_ATTRS)
def test_delete_factory_attr_is_blocked(attr):
    conn = connect(":memory:")
    with pytest.raises(AttributeError) as exc:
        delattr(conn, attr)
    assert "禁止删除" in str(exc.value)
    # 拦截之后连接必须仍然可用（槽位没被破坏）
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    conn.close()


def test_explicit_assignment_still_works():
    """正常赋值路径不能被误伤：row_factory 可设可换回 None，text_factory 同理。"""
    conn = connect(":memory:", row_factory=sqlite3.Row)
    assert conn.row_factory is sqlite3.Row
    conn.row_factory = None
    assert conn.row_factory is None
    conn.text_factory = bytes
    assert conn.text_factory is bytes
    conn.text_factory = str
    assert conn.text_factory is str
    conn.close()


def test_row_factory_kwarg_is_applied():
    conn = connect(":memory:", row_factory=sqlite3.Row)
    conn.execute("CREATE TABLE t (a, b)")
    conn.execute("INSERT INTO t VALUES (1, 2)")
    conn.commit()
    row = conn.execute("SELECT a, b FROM t").fetchone()
    assert row["a"] == 1 and row["b"] == 2  # 只有 sqlite3.Row 才支持按键取值
    conn.close()


def test_other_attributes_deletion_unaffected():
    """只拦工厂属性，其它属性删除照旧（不扩大影响面）。"""
    conn = connect(":memory:")
    conn.custom_marker = 1
    del conn.custom_marker
    assert not hasattr(conn, "custom_marker")
    conn.close()

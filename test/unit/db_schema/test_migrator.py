"""db_schema migrator 单元测试：缺表建 / 缺列补 / 复合约束忽略 / 特殊迁移 / FTS5 虚拟表 / 只读 diff。"""
import sqlite3
import pytest

from utils.db_schema.migrator import ChangeReport, diff_schema, ensure_schema
from utils.db_schema.tables import TableDef


def _conn(tmp_path):
    return sqlite3.connect(str(tmp_path / "t.db"))


def test_change_report_defaults():
    rep = ChangeReport()
    assert rep.created == []
    assert rep.added_columns == {}
    assert rep.migrations == []


def test_create_missing_table(tmp_path):
    td = TableDef(name="users", ddl="CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn = _conn(tmp_path)
    rep = ensure_schema(conn, [td])
    assert "users" in rep.created
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "users" in names


def test_add_missing_column(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.commit()
    td = TableDef(name="users", ddl="""CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT
)""")
    rep = ensure_schema(conn, [td])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    assert "name" in cols
    assert "users" in rep.added_columns
    # 幂等：再跑一遍无变更
    rep2 = ensure_schema(conn, [td])
    assert not rep2.created
    assert not rep2.added_columns


def test_ignore_compound_constraint_lines(tmp_path):
    # PRIMARY KEY(a,b) / UNIQUE(v) 等复合约束行不得被当作缺失列
    ddl = ("CREATE TABLE t (a TEXT NOT NULL, b TEXT NOT NULL, v REAL, "
           "PRIMARY KEY (a, b), UNIQUE (v))")
    td = TableDef(name="t", ddl=ddl)
    conn = _conn(tmp_path)
    ensure_schema(conn, [td])
    ensure_schema(conn, [td])  # 第二遍不应试图 ALTER PRIMARY KEY 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    assert cols == ["a", "b", "v"]


def test_run_special_migration_each_call(tmp_path):
    calls = []

    def m(conn):
        calls.append(1)

    td = TableDef(name="t", ddl="CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn = _conn(tmp_path)
    ensure_schema(conn, [td], special_migrations=[m])
    ensure_schema(conn, [td], special_migrations=[m])
    assert len(calls) == 2  # 特殊迁移按注册执行（自身保证幂等）


def test_virtual_table_skips_diff(tmp_path):
    td = TableDef(name="f", ddl="CREATE VIRTUAL TABLE f USING fts5(x)", virtual=True)
    conn = _conn(tmp_path)
    rep = ensure_schema(conn, [td])
    assert "f" in rep.created
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "f" in names
    # 已存在的虚拟表再次执行不报错、不重复建
    rep2 = ensure_schema(conn, [td])
    assert "f" not in rep2.created


def test_fts_virtual_shadow_tables_not_treated_as_business(tmp_path):
    # fts5 影子表由引擎托管，diff 不应把它们当业务表 diff
    td = TableDef(name="semantic_memory_fts",
                  ddl="CREATE VIRTUAL TABLE IF NOT EXISTS semantic_memory_fts "
                      "USING fts5(entry_id, content)", virtual=True)
    conn = _conn(tmp_path)
    ensure_schema(conn, [td])
    ensure_schema(conn, [td])  # 幂等不报错


def test_diff_schema_is_readonly(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE k (k TEXT)")
    conn.commit()
    td = TableDef(name="k", ddl="""CREATE TABLE IF NOT EXISTS k (
    k TEXT,
    d TEXT
)""")
    rep = diff_schema(conn, [td])  # 只读：报告缺列，不执行
    assert "k" in rep.added_columns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(k)")]
    assert "d" not in cols  # 未真正写入
    # 全空库 → 报告将建表
    conn2 = _conn(tmp_path.parent)
    rep2 = diff_schema(conn2, [td])
    assert "k" in rep2.created


def test_if_not_exists_ddl_ignored_in_column_parse(tmp_path):
    td = TableDef(name="k", ddl="""CREATE TABLE IF NOT EXISTS k (
    k TEXT,
    d TEXT
)""")
    conn = _conn(tmp_path)
    ensure_schema(conn, [td])
    # 已有表缺一列 → 只补该列
    td2 = TableDef(name="k", ddl="""CREATE TABLE IF NOT EXISTS k (
    k TEXT,
    d TEXT,
    e TEXT
)""")
    rep = ensure_schema(conn, [td2])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(k)")]
    assert "e" in cols and "k" in rep.added_columns

def test_comment_lines_do_not_hide_columns(tmp_path):
    # 真 bug 回归：列定义紧跟 -- 注释行时，注释与列名同属一个顶层片段，
    # 片段以 -- 开头会让列名正则匹配不到 → 该列被整段吞掉，diff 报「无差异」、
    # ensure_schema 永远不补列（实测 strategy_run.trace_run_id 就这样从生产库消失）。
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    td = TableDef(name="t", ddl="""CREATE TABLE t (
    id INTEGER PRIMARY KEY,
    -- C 批：策略研发自建的 trace run_id。
    -- 第二行注释（含逗号, 也要一起忽略）
    trace_run_id TEXT DEFAULT '',
    created_at TEXT
)""")
    rep = ensure_schema(conn, [td])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    assert "trace_run_id" in cols
    assert rep.added_columns["t"] == ["trace_run_id", "created_at"]
    # 只读 diff 同样要看得见（--check 路径）
    assert diff_schema(conn, [td]).added_columns == {}


def test_trailing_comment_not_leaked_into_alter(tmp_path):
    # 行尾注释（含逗号）不得破坏切分，也不得被写进 ALTER 语句
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE k (k TEXT)")
    conn.commit()
    td = TableDef(name="k", ddl="""CREATE TABLE k (
    k TEXT,
    n INTEGER DEFAULT 0,  -- 新增列（行尾注释, 含逗号）
    m TEXT
)""")
    rep = ensure_schema(conn, [td])
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(k)")}
    assert set(rep.added_columns["k"]) == {"n", "m"}
    assert info["n"][2] == "INTEGER" and info["n"][4] == "0"
    assert info["m"][2] == "TEXT"
"""CLI 端到端测试：空库建表 / check 只读检测 / 幂等。"""
import sqlite3

from utils.db_schema.cli_core import main, run_init


def test_run_init_apply_builds_tables(tmp_path):
    paths = {"stock_radar": str(tmp_path / "stock_radar.db"),
             "market_cache": str(tmp_path / "market_cache.db")}
    reps = run_init(paths=paths, only=["stock_radar", "market_cache"], apply=True)
    conn = sqlite3.connect(paths["stock_radar"])
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='web_dialogs'").fetchone()
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dialog'").fetchone()
    finally:
        conn.close()
    assert reps["stock_radar"].created  # 新库全部表被建
    assert "dialog" in reps["stock_radar"].created


def test_check_mode_detects_missing_column_without_writing(tmp_path):
    db = tmp_path / "stock_radar.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE web_dialogs (id INTEGER PRIMARY KEY)")  # 缺创建列
    conn.commit()
    conn.close()
    paths = {"stock_radar": str(db)}
    reps = run_init(paths=paths, only=["stock_radar"], apply=False)
    assert "web_dialogs" in reps["stock_radar"].added_columns
    # 未真正写入
    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(web_dialogs)")]
    finally:
        conn.close()
    assert cols == ["id"]


def test_run_init_idempotent(tmp_path):
    paths = {"stock_radar": str(tmp_path / "stock_radar.db")}
    r1 = run_init(paths=paths, only=["stock_radar"], apply=True)
    r2 = run_init(paths=paths, only=["stock_radar"], apply=True)
    assert not r2["stock_radar"].created
    assert not r2["stock_radar"].added_columns


def test_main_check_exit_code(tmp_path, monkeypatch):
    import utils.db_schema.cli_core as core
    # apply 后 check 应无差异（退出码 0）
    monkeypatch.setattr(core, "run_init", lambda **kw: {"x": type(
        "R", (), {"created": [], "added_columns": {}})()})
    assert main(["--check"]) == 0
    # 有差异时 check 退出码 1
    monkeypatch.setattr(core, "run_init", lambda **kw: {"x": type(
        "R", (), {"created": ["t"], "added_columns": {}})()})
    assert main(["--check"]) == 1


def test_main_list(tmp_path, capsys):
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "backtest" in out and "stock_radar" in out
"""指标口径标记（engine_version）+ mul 倍数写法约束。

背景：2026-10-18 年化口径从「rnorm100（bar 数 252/年）」改为「自然日 CAGR」，
同一策略口径切换前后年化不可比。用户要求给 `strategy_run` 加口径标记，
否则会误以为结论变了。同时收紧 `mul` 倍数必须写成 JSON 数字（真机多次写成字符串）。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.db_schema.tables.backtest import TABLES  # noqa: E402


def _ddl(table: str) -> str:
    return next(td.ddl for td in TABLES if td.name == table)


def test_strategy_run_has_engine_version_column():
    ddl = _ddl("strategy_run")
    assert "engine_version" in ddl, "缺口径标记列；老 run 与新 run 将无法区分"


def test_engine_version_constant_exists():
    from backtest.engine import ENGINE_VERSION
    assert ENGINE_VERSION and isinstance(ENGINE_VERSION, str)
    # 版本号应体现口径本身，便于将来再变时区分
    assert "calendar" in ENGINE_VERSION or "annual" in ENGINE_VERSION


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    from config import Config
    db = str(tmp_path / "backtest.db")
    monkeypatch.setattr(Config, "get_backtest_db_path", lambda: db)
    from utils.cache import backtest_db as bd
    bd.init_backtest_tables()
    bd._MISSING_COLS.clear()
    yield db
    bd._MISSING_COLS.clear()


def test_create_run_persists_engine_version(tmp_db):
    from utils.cache.backtest_db import create_strategy_run, get_strategy_run
    create_strategy_run("SR-T-1", "想法", "600519", "2025-01-01~2026-01-01",
                        engine_version="2026-10-18.calendar-annual")
    row = get_strategy_run("SR-T-1")
    assert row["engine_version"] == "2026-10-18.calendar-annual"


def test_create_run_degrades_without_column(tmp_path, monkeypatch):
    """迁移没跑（库里没有该列）时必须降级写入，不能把研究链打断。"""
    from config import Config
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE strategy_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_uuid TEXT UNIQUE NOT NULL,
        idea TEXT NOT NULL, universe TEXT NOT NULL, period TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running', stop_reason TEXT,
        best_iter_id INTEGER, total_iters INTEGER DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'panel', dialog_uuid TEXT,
        trace_run_id TEXT DEFAULT '', created_at TEXT NOT NULL, completed_at TEXT)""")
    con.commit()
    con.close()
    monkeypatch.setattr(Config, "get_backtest_db_path", lambda: db)
    from utils.cache import backtest_db as bd
    bd._MISSING_COLS.clear()

    bd.create_strategy_run("SR-T-2", "想法", "600519", "2025-01-01~2026-01-01",
                           engine_version="v9")
    row = bd.get_strategy_run("SR-T-2")
    assert row["run_uuid"] == "SR-T-2", "缺列时整条记录都必须写成功"
    bd._MISSING_COLS.clear()


def test_llm_schema_warns_mul_multiplier_must_be_number():
    """mul 倍数写成字符串会被判恒假；schema 必须显式警告（真机踩过 4 次）。"""
    from backtest.json_compiler import export_llm_schema
    text = export_llm_schema()
    assert "mul" in text
    assert "数字" in text and "字符串" in text, "缺「倍数必须是数字、不能写字符串」的约束"

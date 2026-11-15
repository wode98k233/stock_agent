"""数据缺口 → 补数任务闭环单测（Plan 3 任务 5）。

不连真库：回测库指向 tmp_path 下的临时文件；路由层直接调端点函数（不挂 FastAPI）。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    """把回测库指向临时文件并建表（范式同 test_strategy_session_store.py）"""
    from config import Config
    test_db = str(tmp_path / "backtest.db")
    monkeypatch.setattr(Config, "get_backtest_db_path", lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    return test_db


@pytest.fixture()
def tmp_market_db(tmp_path, monkeypatch):
    """market 库也指向临时文件：/stats 会真查 76MB 的 db/market_data.db（慢、且受本机状态影响）。"""
    from config import Config
    test_db = str(tmp_path / "market_data.db")
    monkeypatch.setattr(Config, "get_market_data_db_path", lambda: test_db)
    from utils.cache.market_data_db import init_market_data_tables
    init_market_data_tables()
    return test_db


def _run(coro):
    return asyncio.run(coro)


class _FakeCollector:
    """假采集器：只记调用，不起线程"""

    def __init__(self):
        self.calls = []

    def create_and_run(self, task_type, config):
        self.calls.append((task_type, config))
        return "DT-TEST-0001"


@pytest.fixture()
def route_env(tmp_backtest_db, monkeypatch):
    """端点依赖全部打桩：run 存在、两段缺口、来源写入被记录"""
    from server.routes import data_tasks as mod
    collector = _FakeCollector()
    origins = []
    monkeypatch.setattr(mod, "_get_collector", lambda: collector)
    monkeypatch.setattr(mod, "get_backtest_run",
                        lambda run_id: {"run_id": run_id, "code": "600519"} if run_id == "BT-TEST-0001" else None)
    monkeypatch.setattr(mod, "get_backtest_result",
                        lambda run_id: {"gaps_json": json.dumps([
                            {"start": "2023-10-12", "end": "2023-10-18", "missing": 5},
                            {"start": "2024-01-02", "end": "2024-01-05", "missing": 3},
                        ])})
    monkeypatch.setattr(mod, "set_data_task_origin",
                        lambda task_id, origin, ref_id="": origins.append((task_id, origin, ref_id)))
    return mod, collector, origins


# ── 用例 1-4：_days_covering（纯函数）──

def test_days_covering_uses_natural_day_span_plus_buffer():
    from backtest.data_collector import _days_covering
    assert _days_covering("2025-03-03", "2025-03-07", today="2026-10-11") == 602


def test_days_covering_capped_by_maximum():
    from backtest.data_collector import _days_covering
    assert _days_covering("2001-01-01", today="2026-10-11") == 2000


def test_days_covering_bad_start_falls_back_to_minimum():
    from backtest.data_collector import _days_covering
    assert _days_covering("not-a-date") == 60


def test_days_covering_never_below_minimum():
    from backtest.data_collector import _days_covering
    assert _days_covering("2026-10-09", today="2026-10-11") == 60


# ── 用例 5：POST /from-gap 建任务 ──

def test_from_gap_creates_daily_kline_task(route_env):
    mod, collector, origins = route_env
    out = _run(mod.create_task_from_gap(mod.FromGapRequest(run_id="BT-TEST-0001", gap_index=1)))
    assert out["task_id"] == "DT-TEST-0001"
    assert out["gap"] == {"start": "2024-01-02", "end": "2024-01-05", "missing": 3}
    assert out["days"] >= 60
    assert len(collector.calls) == 1
    task_type, config = collector.calls[0]
    assert task_type == "daily_kline"
    assert config["codes"] == ["600519"]
    assert config["source"] == "auto"
    assert origins == [("DT-TEST-0001", "backtest_gap", "BT-TEST-0001")]


# ── 用例 6：越界 / 无缺口 / run 不存在 ──

def test_from_gap_rejects_out_of_range_index(route_env):
    mod, collector, _ = route_env
    with pytest.raises(Exception) as err:
        _run(mod.create_task_from_gap(mod.FromGapRequest(run_id="BT-TEST-0001", gap_index=9)))
    assert getattr(err.value, "status_code", None) == 400
    assert collector.calls == []


def test_from_gap_rejects_unparsable_gaps(route_env, monkeypatch):
    mod, collector, _ = route_env
    monkeypatch.setattr(mod, "get_backtest_result", lambda run_id: {"gaps_json": "{not json"})
    with pytest.raises(Exception) as err:
        _run(mod.create_task_from_gap(mod.FromGapRequest(run_id="BT-TEST-0001")))
    assert getattr(err.value, "status_code", None) == 400
    assert collector.calls == []


def test_from_gap_rejects_unknown_run(route_env):
    mod, collector, _ = route_env
    with pytest.raises(Exception) as err:
        _run(mod.create_task_from_gap(mod.FromGapRequest(run_id="BT-NOPE")))
    assert getattr(err.value, "status_code", None) == 404
    assert collector.calls == []


# ── 用例 7：来源计数忽略历史行 ──

def test_count_tasks_by_origin_ignores_legacy_rows(tmp_backtest_db):
    from utils.cache.backtest_db import create_data_task, set_data_task_origin
    from server.routes.data_tasks import _count_tasks_by_origin
    from utils.cache.backtest_db import get_backtest_db

    create_data_task("DT-LEGACY-1", "daily_kline", json.dumps({"codes": ["600519"]}))
    create_data_task("DT-GAP-1", "daily_kline", json.dumps({"codes": ["600519"]}))
    set_data_task_origin("DT-GAP-1", origin="backtest_gap", ref_id="BT-TEST-0001")
    assert _count_tasks_by_origin("backtest_gap") == 1

    # 老任务 origin 为 NULL 时也不计入
    with get_backtest_db() as conn:
        conn.execute("UPDATE data_tasks SET origin = NULL WHERE task_id = ?", ("DT-LEGACY-1",))
    assert _count_tasks_by_origin("backtest_gap") == 1
    assert _count_tasks_by_origin("manual") == 0


def test_stats_reports_backtest_gap_count(tmp_backtest_db, tmp_market_db, monkeypatch):
    """GET /stats 加性字段：不影响既有键，补数计数进返回体"""
    from server.routes import data_tasks as mod
    monkeypatch.setattr(mod, "_count_tasks_by_origin", lambda origin: 7)
    out = _run(mod.data_stats())
    assert out["backtest_gap_tasks"] == 7
    assert "tables" in out and "quality" in out

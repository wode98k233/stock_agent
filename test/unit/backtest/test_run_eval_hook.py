"""评判挂点单测（Plan 3 任务 4）：自动评判调度、GET 详情 eval 块、rerun 端点。

不连真库：回测库指向 tmp_path 下的临时文件；端点函数直接 await 调用（不挂 FastAPI）。
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
    """把回测库指向临时文件并建表（范式同 test_data_gap_tasks.py）"""
    from config import Config
    test_db = str(tmp_path / "backtest.db")
    monkeypatch.setattr(Config, "get_backtest_db_path", lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    return test_db


def _run(coro):
    return asyncio.run(coro)


class _FakeThread:
    """假线程：只记参数，不起真线程（断言可判定）"""

    calls = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=""):
        _FakeThread.calls.append({"target": target, "args": args,
                                  "kwargs": kwargs or {}, "name": name})

    def start(self):
        pass


@pytest.fixture(autouse=True)
def _reset_fake_thread():
    _FakeThread.calls = []
    yield
    _FakeThread.calls = []


def _cfg():
    """create_backtest_run 的必填项：start_date / end_date 是硬要求"""
    return {"code": "600519", "strategy_id": "dual_ma_crossover",
            "start_date": "2023-01-01", "end_date": "2024-01-01"}


def _healthy_run():
    return {"run_id": "BT-1", "strategy_id": "dual_ma_crossover", "code": "600519",
            "start_date": "2023-01-01", "end_date": "2024-01-01", "initial_cash": 1000000}


# ── 用例 1-3：schedule_isoos_eval 的开关 ──

def test_schedules_eval_thread_for_healthy_run(monkeypatch):
    from backtest import eval_runner
    monkeypatch.setattr(eval_runner.threading, "Thread", _FakeThread)
    monkeypatch.setattr(eval_runner, "load_strategy_json", lambda sid: "{\"name\": \"x\"}")
    thread = eval_runner.schedule_isoos_eval(_healthy_run(), {"health": "ok"})
    assert thread is not None
    assert len(_FakeThread.calls) == 1
    assert _FakeThread.calls[0]["name"] == "backtest-eval-BT-1"
    assert _FakeThread.calls[0]["kwargs"]["entry_timing"] == "next_open"


def test_skips_eval_when_health_is_data_gap(monkeypatch):
    from backtest import eval_runner
    monkeypatch.setattr(eval_runner.threading, "Thread", _FakeThread)
    monkeypatch.setattr(eval_runner, "load_strategy_json", lambda sid: "{\"name\": \"x\"}")
    assert eval_runner.schedule_isoos_eval(_healthy_run(), {"health": "data_gap"}) is None
    assert _FakeThread.calls == []


def test_skips_eval_when_auto_eval_disabled(monkeypatch):
    from backtest import eval_runner
    from config import Config
    monkeypatch.setattr(eval_runner.threading, "Thread", _FakeThread)
    monkeypatch.setattr(eval_runner, "load_strategy_json", lambda sid: "{\"name\": \"x\"}")
    monkeypatch.setattr(Config, "BACKTEST_AUTO_EVAL", False)
    assert eval_runner.schedule_isoos_eval(_healthy_run(), {"health": "ok"}) is None
    assert _FakeThread.calls == []


# ── 用例 4：GET /runs/{run_id} 的 eval 块 ──

def test_run_detail_returns_parsed_eval(tmp_backtest_db, monkeypatch):
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result, save_backtest_eval
    from server.routes import backtest as mod
    monkeypatch.setattr(mod, "_resolve_stock_name", lambda code: "测试股")
    create_backtest_run("BT-EVAL-1", _cfg())
    save_backtest_result("BT-EVAL-1", {"health": "ok", "trade_count": 3})
    save_backtest_eval("BT-EVAL-1", {
        "verdict": "ok", "is_sharpe": 1.2, "oos_sharpe": 1.0, "sharpe_ratio_io": 1.2,
        "split_date": "2023-07-01",
        "is_metrics_json": json.dumps({"sharpe": 1.2}),
        "oos_metrics_json": json.dumps({"sharpe": 1.0}),
    })
    out = _run(mod.get_run_detail("BT-EVAL-1"))
    assert out["eval"]["is_sharpe"] == 1.2
    assert out["eval"]["is_metrics"] == {"sharpe": 1.2}
    assert out["eval"]["oos_metrics"] == {"sharpe": 1.0}


def test_run_detail_eval_is_none_without_eval_row(tmp_backtest_db, monkeypatch):
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result
    from server.routes import backtest as mod
    monkeypatch.setattr(mod, "_resolve_stock_name", lambda code: "测试股")
    create_backtest_run("BT-NOEVAL-1", _cfg())
    save_backtest_result("BT-NOEVAL-1", {"health": "ok", "trade_count": 3})
    out = _run(mod.get_run_detail("BT-NOEVAL-1"))
    assert out["eval"] is None


# ── 用例 5-6：POST /runs/{run_id}/rerun ──

def test_rerun_creates_new_run_pointing_back(tmp_backtest_db, monkeypatch):
    from utils.cache.backtest_db import create_backtest_run, get_backtest_run
    from server.routes import backtest as mod
    create_backtest_run("BT-ORIG-1", _cfg(), origin="manual")
    monkeypatch.setattr(mod.threading, "Thread", _FakeThread)
    started = []
    monkeypatch.setattr(mod, "_run_backtest_async",
                        lambda run_id, config: started.append(run_id))

    out = _run(mod.rerun_backtest("BT-ORIG-1", mod.RerunRunRequest(reason="data_gap_healed")))
    assert out["run_id"] != "BT-ORIG-1"
    assert out["rerun_of"] == "BT-ORIG-1"
    assert out["reason"] == "data_gap_healed"
    new_row = get_backtest_run(out["run_id"])
    assert new_row["ref_id"] == "BT-ORIG-1"
    assert new_row["origin"] == "manual"
    assert len(_FakeThread.calls) == 1
    assert _FakeThread.calls[0]["args"][0] == out["run_id"]
    # 手动驱动一次假线程体：证明重跑确实会调回测执行函数（且只调一次）
    _FakeThread.calls[0]["target"](*_FakeThread.calls[0]["args"])
    assert started == [out["run_id"]]


def test_rerun_rejects_unparsable_config(tmp_backtest_db, monkeypatch):
    from utils.cache.backtest_db import (create_backtest_run, get_backtest_db,
                                         get_backtest_runs)
    from server.routes import backtest as mod
    create_backtest_run("BT-BADCFG-1", _cfg())
    with get_backtest_db() as conn:
        conn.execute("UPDATE backtest_runs SET config_json = ? WHERE run_id = ?",
                     ("{not json", "BT-BADCFG-1"))
    before = get_backtest_runs(limit=100)[1]
    with pytest.raises(Exception) as err:
        _run(mod.rerun_backtest("BT-BADCFG-1", mod.RerunRunRequest()))
    assert getattr(err.value, "status_code", None) == 400
    assert get_backtest_runs(limit=100)[1] == before


def test_rerun_unknown_run_is_404(tmp_backtest_db):
    from server.routes import backtest as mod
    with pytest.raises(Exception) as err:
        _run(mod.rerun_backtest("BT-NOPE", mod.RerunRunRequest()))
    assert getattr(err.value, "status_code", None) == 404

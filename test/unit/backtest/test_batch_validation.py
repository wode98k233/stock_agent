"""批量验证单测（Plan 3 任务 8）：三档口径统计 + 体检缺失处理 + 换标的/换区间重跑。

不连真库、不联网：回测库指向 tmp_path 下的临时文件；端点函数直接 await 调用（不挂 FastAPI）。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BATCH_ID = "BATCH-1"


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    """把回测库指向临时文件并建表（范式同 test_run_eval_hook.py）"""
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


def _patch_names(monkeypatch, mod):
    monkeypatch.setattr(mod, "_lookup_stock_names",
                        lambda codes: {c: f"票{c}" for c in codes})


def _make_batch(codes, batch_id=BATCH_ID):
    """建批次 + 明细（明细初始 pending）"""
    from utils.cache.backtest_db import create_batch, create_batch_items
    create_batch(batch_id, "dual_ma_crossover", "双均线交叉", {"fast": 5}, {"code": "池"},
                 list(codes), 1000000.0, "2023-01-01", "2024-01-01")
    create_batch_items(batch_id, [{"code": c, "asset_type": "stock", "run_id": f"BT-{c}"}
                                  for c in codes])


def _finish(code, *, health="ok", win_rate=0.6, trade_count=8, sharpe_ratio=1.0,
            total_return=0.2, status="completed", batch_id=BATCH_ID):
    """把一只票的明细标成终态，并写入体检结论（health 只从 backtest_results 取）"""
    from utils.cache.backtest_db import get_backtest_db, update_batch_item
    update_batch_item(batch_id, code, status=status, run_id=f"BT-{code}",
                      total_return=total_return, annual_return=0.1,
                      sharpe_ratio=sharpe_ratio, max_drawdown=-0.15,
                      win_rate=win_rate, trade_count=trade_count)
    with get_backtest_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backtest_results "
            "(run_id, health, total_return, annual_return, sharpe_ratio, max_drawdown, "
            " trade_count, win_rate, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"BT-{code}", health, total_return, 0.1, sharpe_ratio, -0.15,
             trade_count, win_rate, "2024-01-01 00:00:00"))


# ---------------------------------------------------------------- _median 口径

def test_median_ignores_missing_and_non_numeric():
    from server.routes.backtest import _median
    assert _median([]) is None
    assert _median([None, "x"]) is None
    assert _median([1, 2, 3]) == 2.0
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([None, 1, "x", 3]) == 2.0


# ---------------------------------------------------------------- 三档统计

def test_valid_and_weak_counts_split(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    valid = [f"60000{i}" for i in range(1, 10)]      # 9 只达标
    weak = [f"0000{i:02d}" for i in range(1, 15)]    # 14 只有结果但不达标
    _make_batch(valid + weak)
    for code in valid:
        _finish(code, win_rate=0.6, trade_count=8)
    for code in weak:
        _finish(code, win_rate=0.4, trade_count=8)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["code_count"] == 23
    assert out["completed_count"] == 23
    assert out["valid_count"] == 9
    assert out["weak_count"] == 14
    assert out["unscored_count"] == 0
    assert len(out["items"]) == 23
    assert out["items"][0]["name"] == "票600001"


def test_small_sample_is_weak_not_valid(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519"])
    _finish("600519", win_rate=0.8, trade_count=4)   # 胜率高但样本不足 5 笔

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["valid_count"] == 0
    assert out["weak_count"] == 1

def test_legacy_percent_win_rate_is_normalized(tmp_backtest_db, monkeypatch):
    """存量百分数刻度（9.0 = 9%）不能当成 900% 的胜率算进「有效」档。

    2026-07 那批存量 backtest_results / batch_item 的 win_rate 是百分数（引擎当时口径），
    现口径是 0~1 小数。不归一 → 9% 被算成「胜率 > 50%」，批量验证卡给出错误结论。
    """
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519", "000001"])
    _finish("600519", win_rate=9.0, trade_count=10)    # 存量刻度：9% → 只能算「弱」
    _finish("000001", win_rate=0.6, trade_count=10)   # 现刻度：60% → 「有效」

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["valid_count"] == 1
    assert out["weak_count"] == 1
    rates = {it["code"]: it["win_rate"] for it in out["items"]}
    assert rates["600519"] == pytest.approx(0.09)
    assert rates["000001"] == pytest.approx(0.6)


def test_low_sample_health_can_still_be_valid(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519"])
    _finish("600519", health="low_sample", win_rate=0.6, trade_count=6)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["valid_count"] == 1
    assert out["unscored_count"] == 0


def test_unscorable_health_goes_to_unscored(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519", "000001"])
    _finish("600519", health="data_gap", win_rate=0.9, trade_count=20)
    _finish("000001", health="insufficient_bars", win_rate=0.1, trade_count=1)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["unscored_count"] == 2
    assert out["valid_count"] == 0
    assert out["weak_count"] == 0
    assert out["items"][0]["health"] == "data_gap"


def test_medians_ignore_missing_values(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519", "000001", "000002"])
    _finish("600519", sharpe_ratio=1.0, win_rate=0.6)
    _finish("000001", sharpe_ratio=None, win_rate=0.5)
    _finish("000002", sharpe_ratio=2.0, win_rate=0.4)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["median_sharpe"] == 1.5
    assert out["median_win_rate"] == 0.5


# ---------------------------------------------------------------- hint 文案

def test_hint_flags_single_valid_as_overfit(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519", "000001", "000002"])
    _finish("600519", win_rate=0.7, trade_count=9)
    _finish("000001", win_rate=0.3, trade_count=9)
    _finish("000002", win_rate=0.2, trade_count=9)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["valid_count"] == 1
    assert "600519" in out["hint"] and "过拟合" in out["hint"]


def test_hint_empty_when_all_valid(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519", "000001"])
    _finish("600519", win_rate=0.7, trade_count=9)
    _finish("000001", win_rate=0.6, trade_count=9)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["valid_count"] == 2
    assert out["hint"] == ""


def test_hint_when_none_reaches_bar(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519", "000001"])
    _finish("600519", win_rate=0.5, trade_count=9)
    _finish("000001", win_rate=0.2, trade_count=2)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["valid_count"] == 0
    assert "站不住" in out["hint"]


# ---------------------------------------------------------------- 边界

def test_unknown_batch_is_404(tmp_backtest_db):
    from server.routes import backtest as mod
    with pytest.raises(Exception) as err:
        _run(mod.get_batch_validation("BATCH-NOPE"))
    assert getattr(err.value, "status_code", None) == 404


def test_batch_without_completed_items(tmp_backtest_db, monkeypatch):
    from server.routes import backtest as mod
    _patch_names(monkeypatch, mod)
    _make_batch(["600519"])
    _finish("600519", status="failed", win_rate=0.6, trade_count=9)

    out = _run(mod.get_batch_validation(BATCH_ID))
    assert out["code_count"] == 1
    assert out["completed_count"] == 0
    assert out["valid_count"] == 0 and out["weak_count"] == 0
    assert out["median_sharpe"] is None
    assert out["hint"] == ""


# ---------------------------------------------------------------- rerun-with

def _fake_origin_run(monkeypatch, mod, config):
    """假原记录：不建真 run，直接返回一条带 config_json 的行"""
    run = {"run_id": "BT-ORIG-1", "code": config.get("code", ""),
           "config_json": json.dumps(config)}
    monkeypatch.setattr(mod, "get_backtest_run",
                        lambda run_id: run if run_id == "BT-ORIG-1" else None)
    return run


def _capture_new_run(monkeypatch, mod):
    """截获 create_backtest_run 的入参（含 kwargs）"""
    captured = {}
    monkeypatch.setattr(mod, "create_backtest_run",
                        lambda run_id, config, **kwargs: captured.update(
                            {"run_id": run_id, "config": config, "kwargs": kwargs}))
    monkeypatch.setattr(mod.threading, "Thread", _FakeThread)
    monkeypatch.setattr(mod, "_lookup_stock_names", lambda codes: {codes[0]: "贵州茅台"})
    monkeypatch.setattr(mod, "_auto_detect_asset_type", lambda code: "stock")
    return captured


def test_rerun_with_overrides_code_and_keeps_identity(monkeypatch):
    from server.routes import backtest as mod
    _fake_origin_run(monkeypatch, mod, {
        "code": "600519", "strategy_id": "dual_ma_crossover",
        "start_date": "2023-01-01", "end_date": "2024-01-01", "initial_cash": 1000000})
    captured = _capture_new_run(monkeypatch, mod)

    out = _run(mod.rerun_with("BT-ORIG-1", mod.RerunWithRequest(code="000001")))

    assert captured["config"]["code"] == "000001"
    assert captured["config"]["stock_name"] == "贵州茅台"
    assert captured["config"]["asset_type"] == "stock"
    assert captured["config"]["ref_id"] == "BT-ORIG-1"
    assert captured["config"]["origin"] == "manual"
    assert captured["kwargs"] == {"origin": "manual", "ref_id": "BT-ORIG-1"}
    # 没传区间 → 沿用原区间（否则两次区间不同就没法比）
    assert captured["config"]["start_date"] == "2023-01-01"
    assert captured["config"]["end_date"] == "2024-01-01"
    assert captured["run_id"].startswith("BT-")
    assert out["run_id"] == captured["run_id"]
    assert out["rerun_of"] == "BT-ORIG-1"
    assert out["code"] == "000001"
    # 线程体确实会调到执行函数
    assert _FakeThread.calls[0]["target"] is mod._run_backtest_async
    assert _FakeThread.calls[0]["args"] == (captured["run_id"], captured["config"])


def test_rerun_with_falls_back_to_original_code_and_overrides_dates(monkeypatch):
    from server.routes import backtest as mod
    _fake_origin_run(monkeypatch, mod, {
        "code": "600519", "strategy_id": "dual_ma_crossover",
        "start_date": "2023-01-01", "end_date": "2024-01-01"})
    captured = _capture_new_run(monkeypatch, mod)

    out = _run(mod.rerun_with("BT-ORIG-1", mod.RerunWithRequest(
        start_date="2024-01-01", end_date="2024-06-01")))

    assert captured["config"]["code"] == "600519"
    assert captured["config"]["start_date"] == "2024-01-01"
    assert captured["config"]["end_date"] == "2024-06-01"
    assert out["code"] == "600519"


def test_rerun_with_without_any_code_is_400(monkeypatch):
    from server.routes import backtest as mod
    _fake_origin_run(monkeypatch, mod, {"strategy_id": "dual_ma_crossover"})
    monkeypatch.setattr(mod, "create_backtest_run",
                        lambda *a, **kw: pytest.fail("缺 code 时不该建新 run"))

    with pytest.raises(Exception) as err:
        _run(mod.rerun_with("BT-ORIG-1", mod.RerunWithRequest()))
    assert getattr(err.value, "status_code", None) == 400


def test_rerun_with_unknown_run_is_404(monkeypatch):
    from server.routes import backtest as mod
    monkeypatch.setattr(mod, "get_backtest_run", lambda run_id: None)
    with pytest.raises(Exception) as err:
        _run(mod.rerun_with("BT-NOPE", mod.RerunWithRequest(code="000001")))
    assert getattr(err.value, "status_code", None) == 404

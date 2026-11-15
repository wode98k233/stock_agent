"""回测账本 + 体检的集成测试（设计文档 §3 / §4 / §4.6）"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """把回测库指向临时文件并建表（范式同 test_backtest_engine.py）"""
    test_db = str(tmp_path / "backtest.db")
    with patch("config.Config.get_backtest_db_path", return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _columns(table: str) -> set:
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        rows = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
    return {row[1] for row in rows}  # PRAGMA table_info: 索引 1 是列名


def _base_config(strategy_id: str = "dual_ma_crossover", **overrides) -> dict:
    config = {
        "code": "600519", "strategy_id": strategy_id, "params": {},
        "start_date": "2025-01-01", "end_date": "2025-12-31",
        "initial_cash": 200000, "commission": 0.0003, "min_commission": 5.0,
        "stamp_tax": 0.0005, "transfer_fee": 0.00001, "slippage": 0.001,
        "t_plus_1": True, "lot_size": 100, "adjust_type": "none",
        "stop_loss": 0, "take_profit": 0, "trailing_stop": 0,
        "position_mode": "full", "position_size": 1.0, "asset_type": "stock",
    }
    config.update(overrides)
    return config


def _market_frame(closes) -> pd.DataFrame:
    """构造行情 DataFrame（列名与 data_adapter 期望一致）"""
    days = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({
        # 用字符串日期：与 _load_stock_daily 的真返回值一致（内联执行的 SQL 读出即字符串），
        # 否则 qfq 复权路径 merge 复权因子时会 datetime64 vs str 冲突
        "trade_date": days.strftime("%Y-%m-%d"),
        "open": list(closes),
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": list(closes),
        "volume": [1000000.0] * len(closes),
    })


def _fixture_closes(pattern: str = "sine", bars: int = 200):
    """sine：必然产生均线交叉；flat：恒定价，必然零交易"""
    if pattern == "sine":
        return 100 + 20 * np.sin(np.linspace(0, 8 * np.pi, bars))
    return np.full(bars, 100.0)


def _baseline_dates(bars: int = 200) -> list:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range("2025-01-01", periods=bars, freq="B")]


def test_backtest_runs_has_origin_and_ref_id(setup_test_env):
    cols = _columns("backtest_runs")
    assert "origin" in cols, "backtest_runs 缺少来源列，无法回答这次回测是谁发起的"
    assert "ref_id" in cols


def test_create_backtest_run_records_origin_and_ref_id(setup_test_env):
    from utils.cache.backtest_db import create_backtest_run, get_backtest_run
    create_backtest_run("BT-20261011-AAAAAA", _base_config(),
                        origin="batch", ref_id="BTB-20261011-BBBBBB")
    run = get_backtest_run("BT-20261011-AAAAAA")
    assert run["origin"] == "batch"
    assert run["ref_id"] == "BTB-20261011-BBBBBB"


def test_create_backtest_run_defaults_to_manual(setup_test_env):
    from utils.cache.backtest_db import create_backtest_run, get_backtest_run
    create_backtest_run("BT-20261011-CCCCCC", _base_config())
    run = get_backtest_run("BT-20261011-CCCCCC")
    assert run["origin"] == "manual"
    assert run["ref_id"] == ""


def test_engine_creates_missing_run_record_and_keeps_origin(setup_test_env):
    """研发链路历史上不建 run 记录 → 引擎必须自己补，并带上 origin/ref_id"""
    from backtest.engine import BacktestEngine
    from utils.cache.backtest_db import get_backtest_run
    with patch("backtest.data_adapter._load_stock_daily",
               side_effect=ValueError("无数据: 600519 (2025-01-01 ~ 2025-12-31)")):
        with pytest.raises(ValueError, match="无数据"):
            BacktestEngine().run(_base_config(), run_id="SR-IS-sr_probe",
                                 origin="research", ref_id="SR-20261011-AAAAAA")
    run = get_backtest_run("SR-IS-sr_probe")
    assert run is not None, "引擎必须自己建账，否则 save_backtest_result 会写出孤儿结果"
    assert run["origin"] == "research"
    assert run["ref_id"] == "SR-20261011-AAAAAA"
    assert run["status"] == "failed"


def test_engine_does_not_overwrite_existing_manual_origin(setup_test_env):
    from backtest.engine import BacktestEngine
    from utils.cache.backtest_db import create_backtest_run, get_backtest_run
    create_backtest_run("BT-20261011-DDDDDD", _base_config())
    with patch("backtest.data_adapter._load_stock_daily",
               side_effect=ValueError("无数据: 600519 (2025-01-01 ~ 2025-12-31)")):
        with pytest.raises(ValueError):
            BacktestEngine().run(_base_config(), run_id="BT-20261011-DDDDDD")
    run = get_backtest_run("BT-20261011-DDDDDD")
    assert run["origin"] == "manual", "已存在的记录不该被兜底逻辑改来源"


def test_iso_os_runner_writes_research_ledger(setup_test_env):
    """研发期回测必须进账本（修复 SR- 孤儿的老问题）"""
    from backtest.strategy_research import IsoOsRunner
    from utils.cache.backtest_db import get_backtest_runs
    dual_ma = (PROJECT_ROOT / "backtest" / "strategies" / "json_templates" / "dual_ma_crossover.json").read_text(encoding="utf-8")
    frame = _market_frame(_fixture_closes("sine"))
    runner = IsoOsRunner(split_ratio=0.7, run_uuid="SR-20261011-EEEEEE")
    with patch("backtest.data_adapter._load_stock_daily", return_value=frame):
        runner.run_both(dual_ma, "600519", ("2025-01-01", "2025-09-30"))
    runs, _total = get_backtest_runs(limit=20)
    research_runs = [r for r in runs if r.get("origin") == "research"]
    assert research_runs, "研发期回测没进账本，就是 26 条孤儿的老问题"
    assert all(r["ref_id"] == "SR-20261011-EEEEEE" for r in research_runs)

def test_backtest_results_has_health_columns(setup_test_env):
    cols = _columns("backtest_results")
    expected = {
        "health", "health_detail", "data_bars", "expected_bars",
        "coverage_pct", "gaps_json", "trade_sample_ok",
    }
    assert expected <= cols, "缺体检列: " + str(sorted(expected - cols))


def test_save_backtest_result_writes_health(setup_test_env):
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result, get_backtest_result
    create_backtest_run("BT-20261011-FFFFFF", _base_config())
    save_backtest_result("BT-20261011-FFFFFF", {
        "health": "data_gap",
        "health_detail": "数据覆盖 78%，缺 1 段",
        "data_bars": 156, "expected_bars": 200, "coverage_pct": 0.78,
        "gaps_json": json.dumps([{"start": "2025-03-03", "end": "2025-03-07", "missing": 5}]),
        "trade_sample_ok": 0,
    })
    res = get_backtest_result("BT-20261011-FFFFFF")
    assert res["health"] == "data_gap"
    assert res["coverage_pct"] == 0.78
    assert res["trade_sample_ok"] == 0
    assert res["trade_count"] is None, "缺数/失败路径不该把没算出来的指标填成 0"


def test_save_backtest_result_defaults_health_ok(setup_test_env):
    from utils.cache.backtest_db import create_backtest_run, save_backtest_result, get_backtest_result
    create_backtest_run("BT-20261011-GGGGGG", _base_config())
    save_backtest_result("BT-20261011-GGGGGG", {"total_return": 0.1})
    res = get_backtest_result("BT-20261011-GGGGGG")
    assert res["health"] == "ok", "没传 health 的旧调用方按 ok 处理，保证向后兼容"
    assert res["gaps_json"] == "[]"

def _run_engine(run_id: str, frame: pd.DataFrame, baseline: list, config: dict = None):
    from backtest.engine import BacktestEngine
    with (
        patch("backtest.data_adapter._load_stock_daily", return_value=frame),
        patch("backtest.engine._baseline_trading_days", return_value=baseline),
    ):
        return BacktestEngine().run(config or _base_config(), run_id=run_id)


def test_health_ok_when_covered_and_traded(setup_test_env):
    from utils.cache.backtest_db import get_backtest_result
    frame = _market_frame(_fixture_closes("sine"))
    result = _run_engine("BT-HEALTH-OK", frame, _baseline_dates())
    saved = get_backtest_result("BT-HEALTH-OK")
    assert result["trade_count"] > 0, "正弦行情下双均线必须产生交易，否则本用例失去意义"
    assert saved["health"] == "ok"
    assert saved["coverage_pct"] == 1.0
    assert saved["trade_sample_ok"] == (1 if result["trade_count"] >= 5 else 0)


def test_short_window_falls_back_and_reports_insufficient_bars(setup_test_env):
    """存量缺陷：K 线根数 < 指标预热周期时向量化回测直接越界（IndexError）。

    引擎要回退逐根模式重跑，并判 insufficient_bars；塌缩成 error 等于把
    「窗口太短」说成「引擎坏了」。
    """
    from utils.cache.backtest_db import get_backtest_result
    frame = _market_frame(_fixture_closes("sine", bars=15))  # 双均线 MA20 → 预热 20 根
    result = _run_engine("BT-HEALTH-SHORT", frame, _baseline_dates(15))
    saved = get_backtest_result("BT-HEALTH-SHORT")
    assert saved["health"] == "insufficient_bars"
    assert "预热" in saved["health_detail"]
    assert result["trade_count"] == 0
    assert any("窗口过短" in w for w in result["warnings"]), "警告里要说清是窗口太短，不是策略没信号"


def test_progress_callback_fires_for_each_bar(setup_test_env):
    """存量缺陷：feed 在 run() 前 len() 恒为 0，总根数若取自 len(feed)，
    进度观察者就挂不上，单笔回测的进度回调永远不触发。"""
    from backtest.engine import BacktestEngine
    frame = _market_frame(_fixture_closes("sine"))
    seen = []
    with (
        patch("backtest.data_adapter._load_stock_daily", return_value=frame),
        patch("backtest.engine._baseline_trading_days", return_value=_baseline_dates()),
    ):
        BacktestEngine().run(
            _base_config(), run_id="BT-PROGRESS",
            progress_callback=lambda cur, total, pct: seen.append((cur, total, pct)),
        )
    assert seen, "进度回调没被调用：观察者没挂上（总根数取成了 len(feed)=0）"
    assert seen[-1][1] == 200, "总根数应等于源 DataFrame 行数"
    assert seen[-1][2] == 1.0, "最后一根应报 100%"


def test_coverage_meta_calibrates_truncated_baseline(setup_test_env):
    """基准指数只覆盖窗口后半段（真库 000300 只有近两年）时分母要按密度校准，不能顶到 100%"""
    from backtest.engine import _calc_coverage_meta

    class _FakeData:
        class p:
            dataname = pd.DataFrame(index=pd.to_datetime(_baseline_dates(200)))

    # 窗口 2025 全年（工作日估算 260 根），基准只覆盖 2025-03-20 起的 200 个交易日
    truncated = [d.strftime("%Y-%m-%d") for d in pd.date_range("2025-03-20", periods=200, freq="B")]
    with patch("backtest.engine._baseline_trading_days", return_value=truncated):
        meta = _calc_coverage_meta(_FakeData(), "2025-01-01", truncated[-1])
    assert meta["expected_bars"] > len(truncated), "分母必须大于被截断的基准长度"
    assert meta["coverage_pct"] < 1.0, "行情只覆盖基准段时覆盖率不该是 100%"
    assert meta["gaps"] and meta["gaps"][0]["start"] == "2025-01-01", "基准之外的头段要记一条缺口，前端才有的可补"
    assert meta["gaps"][0]["end"] == truncated[0]

def test_health_no_signal_when_flat_market(setup_test_env):
    from utils.cache.backtest_db import get_backtest_result
    frame = _market_frame(_fixture_closes("flat"))
    result = _run_engine("BT-HEALTH-NOSIG", frame, _baseline_dates())
    saved = get_backtest_result("BT-HEALTH-NOSIG")
    assert result["trade_count"] == 0, "恒定价下均线不交叉，必须零交易"
    assert saved["health"] == "no_signal"
    assert saved["trade_sample_ok"] == 0


def test_health_data_gap_when_coverage_below_threshold(setup_test_env):
    from utils.cache.backtest_db import get_backtest_result
    frame = _market_frame(_fixture_closes("sine"))
    baseline = _baseline_dates() + [f'2025-11-{d:02d}' for d in range(3, 23)]
    _run_engine("BT-HEALTH-GAP", frame, baseline)
    saved = get_backtest_result("BT-HEALTH-GAP")
    assert saved["health"] == "data_gap", "覆盖 200/220 必须判缺数（阈值 0.95）"
    assert saved["expected_bars"] == 220 and saved["data_bars"] == 200
    assert json.loads(saved["gaps_json"]), "缺了 20 个基准交易日，gaps_json 不能是空数组"
    assert saved["trade_count"] is not None, "缺数不是失败：回测仍跑完并留下指标"


def test_health_invalid_params_when_cash_below_one_lot(setup_test_env):
    from utils.cache.backtest_db import get_backtest_result, get_backtest_run
    frame = _market_frame(_fixture_closes("sine"))
    with pytest.raises(ValueError, match='资金不足'):
        _run_engine("BT-HEALTH-CASH", frame, _baseline_dates(),
                    _base_config(initial_cash=5000))
    run = get_backtest_run("BT-HEALTH-CASH")
    saved = get_backtest_result("BT-HEALTH-CASH")
    assert run["status"] == "failed"
    assert saved["health"] == "invalid_params"
    assert "资金不足" in saved["health_detail"]
    assert saved["trade_count"] is None


def test_health_bad_strategy_when_condition_unresolvable(setup_test_env):
    from backtest.json_compiler import compile_strategy
    from backtest.strategies import _STRATEGY_REGISTRY
    from utils.cache.backtest_db import get_backtest_result
    malformed = {
        "meta": {"name": "畸形条件探针", "strategy_id": "probe_bad_leaf"},
        "params": {},
        "conditions": {
            "buy": {"logic": "AND", "rules": [
                {"type": "price", "data": "close", "op": ">", "right": {"value": 0.01}}]},
            "sell": {"logic": "AND", "rules": [
                {"type": "price", "data": "close", "op": "<", "right": {"value": 999999}}]},
        },
    }
    _STRATEGY_REGISTRY["probe_bad_leaf"] = compile_strategy(json.dumps(malformed, ensure_ascii=False))
    try:
        frame = _market_frame(_fixture_closes("sine"))
        _run_engine("BT-HEALTH-BAD", frame, _baseline_dates(),
                    _base_config(strategy_id="probe_bad_leaf"))
        saved = get_backtest_result("BT-HEALTH-BAD")
        assert saved["health"] == "bad_strategy", "条件被静默丢弃时，指标再好看也不可信"
        assert "表达不了" in saved["health_detail"]
    finally:
        # 探针策略只属于本用例：留在全局注册表里会污染依赖 get_all_strategies() 的其他用例
        # （如 test_signal_scan_minperiod.py::test_min_bars_required_matches_across_scan_mode）
        _STRATEGY_REGISTRY.pop("probe_bad_leaf", None)


def test_min_coverage_is_configurable(setup_test_env):
    from config import Config
    from utils.cache.backtest_db import get_backtest_result
    frame = _market_frame(_fixture_closes("sine"))
    baseline = _baseline_dates() + [f'2025-11-{d:02d}' for d in range(3, 23)]
    with patch.object(Config, "BACKTEST_MIN_COVERAGE", 0.80):
        _run_engine("BT-HEALTH-CFG", frame, baseline)
    assert get_backtest_result("BT-HEALTH-CFG")["health"] == "ok", "阈值可配置：0.909 覆盖率在 0.80 阈值下应放行"



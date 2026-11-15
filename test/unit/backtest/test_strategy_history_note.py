"""跨 run 记忆：首轮注入同标的历史结论。

用户反馈：「每次都全新的，agent 压根不知道之前做了啥」。
研究循环**内部**已有 append-only 历史（同 run 的 8 轮），但**新 run 从零开始**——
同一标的会被反复拿去试上一次已被证伪的方向。这里锁住三件事：
1. 只取**同标的**（不同标的的历史不污染）；
2. 只取最近 3 条、每条一行（防止把模型锚死在旧结论上）；
3. **只进首轮**（append-only 语义不能被破坏：首轮之后不再插入任何内容）。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _gen():
    from backtest.strategy_research import StrategyGenerator
    return StrategyGenerator(llm=None)


def _rows():
    return [
        {"run_uuid": "SR-A", "universe": "600519", "status": "done",
         "created_at": "2026-10-18 10:00:00", "stop_reason": "",
         "best_oos_metrics": {"sharpe": 0.42}, "best_hypothesis": "上升趋势隔夜"},
        {"run_uuid": "SR-B", "universe": "600519", "status": "failed",
         "created_at": "2026-10-17 09:00:00", "stop_reason": "数据不足：非日线",
         "best_oos_metrics": {}, "best_hypothesis": ""},
        {"run_uuid": "SR-C", "universe": "000905", "status": "done",
         "created_at": "2026-10-16 09:00:00", "stop_reason": "",
         "best_oos_metrics": {"sharpe": 1.1}, "best_hypothesis": "别的标的"},
    ]


def test_history_note_only_same_universe(monkeypatch):
    monkeypatch.setattr("utils.cache.backtest_db.list_strategy_runs", lambda limit=50: _rows())
    text = _gen()._history_note("600519")
    assert "【历史参考】" in text
    assert "SR" not in text  # 不暴露内部 run_uuid
    assert "上升趋势隔夜" in text
    assert "数据不足" in text
    assert "别的标的" not in text, "不同标的的历史不能混进来"


def test_history_note_absent_without_rows(monkeypatch):
    monkeypatch.setattr("utils.cache.backtest_db.list_strategy_runs", lambda limit=50: [])
    assert _gen()._history_note("600519") == ""


def test_history_note_failure_is_silent(monkeypatch):
    def boom(limit=50):
        raise RuntimeError("db down")
    monkeypatch.setattr("utils.cache.backtest_db.list_strategy_runs", boom)
    assert _gen()._history_note("600519") == "", "读历史失败绝不能阻塞生成"


def test_history_note_goes_into_first_prompt_only(monkeypatch):
    monkeypatch.setattr("utils.cache.backtest_db.list_strategy_runs", lambda limit=50: _rows())
    g = _gen()
    first = g._initial_user_prompt("上升趋势尾盘买次日卖", "600519")
    assert "【历史参考】" in first
    # 变异 prompt 不再重复注入（历史已在 messages 里，重复只是白烧 token）
    from backtest.strategy_research import IterRecord
    it = IterRecord(round=1, hypothesis="h", is_metrics={}, oos_metrics={}, overfit_flag=False, verdict="keep")
    mutated = g._round_prompt(it, "600519")
    assert "【历史参考】" not in mutated


def test_history_note_caps_at_three(monkeypatch):
    rows = [{"run_uuid": f"SR-{i}", "universe": "600519", "status": "done",
             "created_at": f"2026-10-{10 + i} 09:00:00", "stop_reason": "",
             "best_oos_metrics": {"sharpe": 0.1 * i}, "best_hypothesis": f"假设{i}"}
            for i in range(6)]
    monkeypatch.setattr("utils.cache.backtest_db.list_strategy_runs", lambda limit=50: rows)
    text = _gen()._history_note("600519")
    assert text.count("｜") >= 3, "三条历史各一行"
    assert text.count("假设") <= 3, "最多 3 条，避免锚定"

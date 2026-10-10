"""server/calendar_service 聚合测试：Web UI 日历 reasoning_tokens 链路（分库形态）。"""
import json
import sqlite3
from unittest.mock import patch

import pytest

from server.calendar_service import build_calendar_payload, load_trace_token_stats
from utils.agent_trace.store import TraceStore, store


class _FakeStorage:
    def __init__(self, run_id: str):
        self._run_id = run_id

    def latest_trace_runs_for_dialogs(self, uuids):
        return {u: self._run_id for u in uuids}


@pytest.fixture
def trace_db_with_reasoning(tmp_path, monkeypatch):
    """临时月库（当月）：1 个 llm step 含 reasoning_tokens；store 路径指向 tmp。"""
    month_dir = tmp_path / "agent_trace"
    month_dir.mkdir()
    db = month_dir / f"agent_trace_{store.current_month()}.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    from utils.agent_trace.models import ensure_month_schema
    ensure_month_schema(conn)
    conn.execute(
        "INSERT INTO runs(id, agent_name, input, status, created_at, duration_ms) "
        "VALUES('run-1', 'a', 'q', 'success', 't', 1000.0)"
    )
    conn.execute(
        "INSERT INTO steps(run_id, event_run_id, step_type, step_name, status, started_at, extra) "
        "VALUES('run-1', 'e1', 'llm', 'model', 'success', 't', ?)",
        (json.dumps({"token_usage": {
            "input_tokens": 100, "output_tokens": 80, "total_tokens": 180,
            "cached_tokens": 20, "reasoning_tokens": 45,
        }}),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(month_dir))
    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: str(tmp_path / "agent_trace.db"))
    yield str(db)
    # 注意：不 close 线程缓存连接——close 会波及同线程内真实库连接的使用方，
    # tmp 连接交给 store 的 LRU 自然淘汰（tmp_path 由 pytest 保留，不删文件）


def test_load_trace_token_stats_includes_reasoning(trace_db_with_reasoning):
    """load_trace_token_stats 返回的 token 统计含 reasoning_tokens。"""
    storage = _FakeStorage("run-1")
    result = load_trace_token_stats(storage, {"2026-08-14": [{"dialog_uuid": "dlg-1"}]})
    stats = result["dlg-1"]
    assert stats["reasoning_tokens"] == 45
    assert stats["total_tokens"] == 180
    assert stats["cached_tokens"] == 20


def test_build_calendar_payload_dialog_has_reasoning(trace_db_with_reasoning):
    """build_calendar_payload 组装 dialog 记录时带上 reasoning_tokens（前端数据源）。"""
    storage = _FakeStorage("run-1")
    with patch("server.calendar_service.load_trading_days", return_value=[]), \
         patch("server.calendar_service.load_dialog_days", return_value={
             "2026-08-14": [{"dialog_uuid": "dlg-1", "created_at": "2026-08-14T01:00:00"}],
         }):
        payload = build_calendar_payload(storage, 2026, 8)

    dlg = payload["dialog_days"]["2026-08-14"][0]
    assert dlg["reasoning_tokens"] == 45
    assert dlg["total_tokens"] == 180

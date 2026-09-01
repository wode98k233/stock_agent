"""server/calendar_service 聚合测试：Web UI 日历 reasoning_tokens 链路。"""
import json
import sqlite3
from unittest.mock import patch

import pytest

from server.calendar_service import build_calendar_payload, load_trace_token_stats


class _FakeStorage:
    def __init__(self, run_id: str):
        self._run_id = run_id

    def latest_trace_runs_for_dialogs(self, uuids):
        return {u: self._run_id for u in uuids}


@pytest.fixture
def trace_db_with_reasoning(tmp_path, monkeypatch):
    """临时 trace 库：1 个 llm step 含 reasoning_tokens。"""
    db = tmp_path / "trace.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, duration_ms REAL)"
    )
    conn.execute(
        "CREATE TABLE steps (run_id TEXT, step_type TEXT, extra TEXT)"
    )
    conn.execute(
        "INSERT INTO runs VALUES ('run-1', 1000.0)"
    )
    conn.execute(
        "INSERT INTO steps VALUES ('run-1', 'llm', ?)",
        (json.dumps({"token_usage": {
            "input_tokens": 100, "output_tokens": 80, "total_tokens": 180,
            "cached_tokens": 20, "reasoning_tokens": 45,
        }}),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "utils.app_paths.get_trace_db_path", lambda: str(db)
    )
    return str(db)


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

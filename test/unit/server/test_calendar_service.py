"""server/calendar_service 聚合测试：Web UI 日历 reasoning_tokens 链路（分库形态）。"""
import json
import sqlite3
from unittest.mock import patch

import pytest

from agents.deep_report.db import DeepReportDB
from agents.deep_report.models import ReportMeta
from server.calendar_service import (build_calendar_payload, load_deep_report_days,
                                     load_trace_token_stats)
from utils.agent_trace.store import TraceStore, store


class _FakeStorage:
    def __init__(self, run_id: str):
        self._run_id = run_id

    def latest_trace_runs_for_dialogs(self, uuids):
        return {u: self._run_id for u in uuids}


class _EmptyStorage:
    def latest_trace_runs_for_dialogs(self, uuids):
        return {}


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


# ── 长报告并入日历（独立字段 deep_reports，不伪造 web_dialogs） ──
def _deep_report_meta(rid: str, created_at: str, *, title: str = "",
                      status: str = "done") -> ReportMeta:
    return ReportMeta(
        rid=rid, universe="银行板块", user_request="", template_id="deep_sector_finance",
        variant="finance", title=title, status=status, created_at=created_at,
        updated_at=created_at,
    )


def test_load_deep_report_days_groups_by_local_date(tmp_path):
    db = DeepReportDB(str(tmp_path / "deep_report.db"))
    db.upsert_report(_deep_report_meta("dr1", "2026-08-14 10:00:00", title="银行深度").to_dict())
    db.upsert_report(_deep_report_meta("dr2", "2026-08-14 23:30:00", title="券商深度",
                                       status="partial").to_dict())
    db.upsert_report(_deep_report_meta("dr3", "2026-08-15 09:00:00").to_dict())
    db.upsert_report(_deep_report_meta("dr4", "2026-07-31 09:00:00").to_dict())
    db.upsert_report(_deep_report_meta("dr5", "2026-09-01 09:00:00").to_dict())

    days = load_deep_report_days(2026, 8, db=db)

    assert sorted(days) == ["2026-08-14", "2026-08-15"]
    assert [r["rid"] for r in days["2026-08-14"]] == ["dr1", "dr2"]
    assert days["2026-08-14"][0]["title"] == "银行深度"
    assert days["2026-08-14"][1]["status"] == "partial"
    assert days["2026-08-15"][0]["rid"] == "dr3"


def test_build_calendar_payload_includes_deep_reports_separately():
    storage = _EmptyStorage()
    with patch("server.calendar_service.load_trading_days", return_value=[]), \
         patch("server.calendar_service.load_dialog_days", return_value={
             "2026-08-14": [{"dialog_uuid": "dlg-1", "title": "对话", "mode": "react_stock",
                             "created_at": "2026-08-14T01:00:00"}],
         }), \
         patch("server.calendar_service.load_deep_report_days", return_value={
             "2026-08-14": [{"rid": "dr1", "title": "银行深度", "status": "done",
                             "created_at": "2026-08-14 10:00:00"}],
         }):
        payload = build_calendar_payload(storage, 2026, 8)

    # 长报告在独立字段里，dialog_days 原样（只有对话，没有伪造记录）
    assert payload["deep_reports"]["2026-08-14"][0]["rid"] == "dr1"
    assert [d["dialog_uuid"] for d in payload["dialog_days"]["2026-08-14"]] == ["dlg-1"]
    assert all("deep_report" not in (d.get("mode") or "") for d in payload["dialog_days"]["2026-08-14"])


def test_load_deep_report_days_missing_db_does_not_create_file(tmp_path, monkeypatch):
    from utils import app_paths

    missing = tmp_path / "no_such_dir" / "deep_report.db"
    monkeypatch.setattr(app_paths, "get_deep_report_db_path", lambda: str(missing))
    assert load_deep_report_days(2026, 8) == {}
    assert not missing.exists()


def test_load_deep_report_days_uses_default_db(tmp_path, monkeypatch):
    """默认路径分支：路径存在时走 get_db()（生产入口，不依赖测试注入 db=）。"""
    path = tmp_path / "deep_report.db"
    db = DeepReportDB(str(path))
    db.upsert_report(_deep_report_meta("dr-default", "2026-08-20 09:00:00").to_dict())

    from utils import app_paths
    import agents.deep_report.db as dbmod
    monkeypatch.setattr(app_paths, "get_deep_report_db_path", lambda: str(path))
    monkeypatch.setattr(dbmod, "get_db", lambda: db)

    days = load_deep_report_days(2026, 8)
    assert [r["rid"] for r in days["2026-08-20"]] == ["dr-default"]

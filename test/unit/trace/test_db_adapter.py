import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

_project_root = str(Path(__file__).parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.agent_trace.db_adapter import SQLiteAdapter


def _tmp_db():
    return str(Path(tempfile.gettempdir()) / f"test_adapter_{threading.get_ident()}_{time.time_ns()}.db")


def _cleanup(db_path):
    try:
        Path(db_path).unlink(missing_ok=True)
    except PermissionError:
        pass


def test_sqlite_adapter_init_schema():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.init_schema()
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        adapter.close()
        assert "runs" in tables
        assert "steps" in tables
        assert "messages" in tables
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_insert_and_get_run():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        run = {"id": "test-run-1", "agent_name": "test", "input": "hello", "status": "running"}
        adapter.insert_run(run)
        result = adapter.get_run("test-run-1")
        adapter.close()
        assert result is not None
        assert result["id"] == "test-run-1"
        assert result["agent_name"] == "test"
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_update_run():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.insert_run({"id": "test-run-2", "agent_name": "test", "input": "", "status": "running"})
        adapter.update_run("test-run-2", status="success", output="done", duration_ms=123.45)
        result = adapter.get_run("test-run-2")
        adapter.close()
        assert result["status"] == "success"
        assert result["duration_ms"] == 123.45
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_insert_step_and_messages():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.insert_run({"id": "test-run-3", "agent_name": "test", "input": "", "status": "running"})
        sid = adapter.insert_step({
            "run_id": "test-run-3",
            "event_run_id": "step-1",
            "parent_run_id": None,
            "step_type": "llm",
            "step_name": "gpt-4o",
            "input": "prompt",
            "extra": {"token_usage": {"input_tokens": 100}},
        })
        assert sid > 0
        adapter.insert_messages([
            {"step_id": sid, "seq": 0, "role": "user", "content": "hello", "tool_calls": None, "tool_call_id": None},
            {"step_id": sid, "seq": 1, "role": "assistant", "content": "world", "tool_calls": None, "tool_call_id": None},
        ])
        steps = adapter.get_steps("test-run-3")
        adapter.close()
        assert len(steps) == 1
        assert steps[0]["step_type"] == "llm"
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_delete_run_cascade():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.insert_run({"id": "test-run-4", "agent_name": "test", "input": "", "status": "running"})
        sid = adapter.insert_step({
            "run_id": "test-run-4",
            "event_run_id": "step-1",
            "step_type": "chain",
            "step_name": "test",
        })
        adapter.insert_messages([
            {"step_id": sid, "seq": 0, "role": "user", "content": "hi"},
        ])
        adapter.delete_run("test-run-4")
        result = adapter.get_run("test-run-4")
        steps = adapter.get_steps("test-run-4")
        adapter.close()
        assert result is None
        assert len(steps) == 0
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_get_steps_with_messages():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.insert_run({"id": "test-run-5", "agent_name": "test", "input": "", "status": "running"})
        sid1 = adapter.insert_step({
            "run_id": "test-run-5",
            "event_run_id": "step-1",
            "step_type": "llm",
            "step_name": "gpt-4o",
        })
        sid2 = adapter.insert_step({
            "run_id": "test-run-5",
            "event_run_id": "step-2",
            "step_type": "tool",
            "step_name": "stock_tool",
        })
        adapter.insert_messages([
            {"step_id": sid1, "seq": 0, "role": "user", "content": "query"},
            {"step_id": sid2, "seq": 0, "role": "tool", "content": "result"},
        ])
        result = adapter.get_steps_with_messages("test-run-5")
        adapter.close()
        assert len(result) == 2
        step1_msgs = [s for s in result if s["event_run_id"] == "step-1"][0]["messages"]
        step2_msgs = [s for s in result if s["event_run_id"] == "step-2"][0]["messages"]
        assert len(step1_msgs) == 1
        assert step1_msgs[0]["role"] == "user"
        assert len(step2_msgs) == 1
        assert step2_msgs[0]["role"] == "tool"
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_get_runs_with_status_filter():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.insert_run({"id": "r1", "agent_name": "test", "input": "", "status": "success"})
        adapter.insert_run({"id": "r2", "agent_name": "test", "input": "", "status": "error"})
        adapter.insert_run({"id": "r3", "agent_name": "test", "input": "", "status": "success"})
        ok_runs = adapter.get_runs(status="success")
        err_runs = adapter.get_runs(status="error")
        adapter.close()
        assert len(ok_runs) == 2
        assert len(err_runs) == 1
    finally:
        _cleanup(db_path)


def test_sqlite_adapter_update_step():
    db_path = _tmp_db()
    try:
        adapter = SQLiteAdapter(db_path)
        adapter.insert_run({"id": "test-run-6", "agent_name": "test", "input": "", "status": "running"})
        sid = adapter.insert_step({
            "run_id": "test-run-6",
            "event_run_id": "step-1",
            "step_type": "chain",
            "step_name": "test",
        })
        adapter.update_step(sid, status="success", output="done", duration_ms=50.0)
        steps = adapter.get_steps("test-run-6")
        adapter.close()
        assert steps[0]["status"] == "success"
        assert steps[0]["duration_ms"] == 50.0
    finally:
        _cleanup(db_path)


if __name__ == "__main__":
    import traceback
    tests = [
        test_sqlite_adapter_init_schema,
        test_sqlite_adapter_insert_and_get_run,
        test_sqlite_adapter_update_run,
        test_sqlite_adapter_insert_step_and_messages,
        test_sqlite_adapter_delete_run_cascade,
        test_sqlite_adapter_get_steps_with_messages,
        test_sqlite_adapter_get_runs_with_status_filter,
        test_sqlite_adapter_update_step,
    ]
    passed, failed = 0, 0
    for fn in tests:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n结果: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)

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
from utils.agent_trace.migrate import TraceExporter, TraceImporter


def _tmp_db():
    return str(Path(tempfile.gettempdir()) / f"test_migrate_{threading.get_ident()}_{time.time_ns()}.db")


def _cleanup(db_path):
    try:
        Path(db_path).unlink(missing_ok=True)
    except PermissionError:
        pass


def _tmp_export_path():
    return str(Path(tempfile.gettempdir()) / f"trace_run_001_{threading.get_ident()}_{time.time_ns()}.json")


def _export_run(exporter):
    return exporter.export_run("run-001", output_path=_tmp_export_path())


def _seed_db(db_path):
    adapter = SQLiteAdapter(db_path)
    adapter.insert_run({
        "id": "run-001",
        "agent_name": "plan",
        "input": "查询贵州茅台",
        "status": "success",
        "created_at": "2026-05-25T09:00:00Z",
        "finished_at": "2026-05-25T09:00:12Z",
        "duration_ms": 12340.0,
        "output": "贵州茅台当前股价 1800.50",
        "error": None,
    })
    sid1 = adapter.insert_step({
        "run_id": "run-001",
        "event_run_id": "step-classifier",
        "parent_run_id": None,
        "step_type": "classifier",
        "step_name": "问题分类",
        "input": '{"query":"查询贵州茅台"}',
        "extra": {"label": "问题分类", "node": "classifier"},
    })
    adapter.update_step(sid1, status="success", output='{"intent":"STOCK_ANALYSIS"}',
                        duration_ms=234.0, finished_at="2026-05-25T09:00:00Z")
    adapter.insert_messages([
        {"step_id": sid1, "seq": 0, "role": "user", "content": "查询贵州茅台",
         "tool_calls": None, "tool_call_id": None},
    ])

    sid2 = adapter.insert_step({
        "run_id": "run-001",
        "event_run_id": "step-llm-1",
        "parent_run_id": "step-classifier",
        "step_type": "llm",
        "step_name": "gpt-4o",
        "input": "[messages]",
        "extra": {"token_usage": {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165}},
    })
    adapter.update_step(sid2, status="success", output='{"intent":"STOCK_ANALYSIS"}',
                        duration_ms=456.0, finished_at="2026-05-25T09:00:01Z")
    adapter.insert_messages([
        {"step_id": sid2, "seq": 0, "role": "user", "content": "查询贵州茅台",
         "tool_calls": None, "tool_call_id": None},
        {"step_id": sid2, "seq": 1, "role": "assistant",
         "content": '{"intent":"STOCK_ANALYSIS"}',
         "tool_calls": None, "tool_call_id": None},
    ])
    adapter.close()
    return db_path


def test_export_single_run():
    db_path = _seed_db(_tmp_db())
    try:
        exporter = TraceExporter(db_path)
        out = _export_run(exporter)
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == "1.0"
        assert "exported_at" in data
        assert data["run"]["id"] == "run-001"
        assert data["run"]["agent_name"] == "plan"
        assert len(data["steps"]) == 2
        step1 = [s for s in data["steps"] if s["event_run_id"] == "step-classifier"][0]
        assert len(step1["messages"]) == 1
        assert step1["messages"][0]["role"] == "user"
        step2 = [s for s in data["steps"] if s["event_run_id"] == "step-llm-1"][0]
        assert len(step2["messages"]) == 2
        Path(out).unlink(missing_ok=True)
    finally:
        _cleanup(db_path)


def test_import_single_run():
    db_path = _seed_db(_tmp_db())
    target_db = _tmp_db()
    try:
        exporter = TraceExporter(db_path)
        out = _export_run(exporter)
        importer = TraceImporter(target_db)
        ok = importer.import_run(out)
        assert ok is True
        adapter = SQLiteAdapter(target_db)
        run = adapter.get_run("run-001")
        assert run is not None
        assert run["agent_name"] == "plan"
        steps = adapter.get_steps("run-001")
        assert len(steps) == 2
        steps_with_msgs = adapter.get_steps_with_messages("run-001")
        step2 = [s for s in steps_with_msgs if s["event_run_id"] == "step-llm-1"][0]
        assert len(step2["messages"]) == 2
        adapter.close()
        Path(out).unlink(missing_ok=True)
    finally:
        _cleanup(db_path)
        _cleanup(target_db)


def test_import_skip_existing():
    db_path = _seed_db(_tmp_db())
    try:
        exporter = TraceExporter(db_path)
        out = _export_run(exporter)
        importer = TraceImporter(db_path)
        ok = importer.import_run(out, strategy="skip")
        assert ok is False
        Path(out).unlink(missing_ok=True)
    finally:
        _cleanup(db_path)


def test_import_replace_existing():
    db_path = _seed_db(_tmp_db())
    try:
        exporter = TraceExporter(db_path)
        out = _export_run(exporter)
        importer = TraceImporter(db_path)
        ok = importer.import_run(out, strategy="replace")
        assert ok is True
        adapter = SQLiteAdapter(db_path)
        run = adapter.get_run("run-001")
        assert run is not None
        adapter.close()
        Path(out).unlink(missing_ok=True)
    finally:
        _cleanup(db_path)


def test_import_error_on_conflict():
    db_path = _seed_db(_tmp_db())
    try:
        exporter = TraceExporter(db_path)
        out = _export_run(exporter)
        importer = TraceImporter(db_path)
        try:
            importer.import_run(out, strategy="error")
            assert False, "应该抛出 ValueError"
        except ValueError as e:
            assert "run-001" in str(e)
        Path(out).unlink(missing_ok=True)
    finally:
        _cleanup(db_path)


def test_export_import_roundtrip():
    src_db = _seed_db(_tmp_db())
    dst_db = _tmp_db()
    try:
        exporter = TraceExporter(src_db)
        out = _export_run(exporter)

        importer = TraceImporter(dst_db)
        ok = importer.import_run(out)
        assert ok is True

        src_adapter = SQLiteAdapter(src_db)
        dst_adapter = SQLiteAdapter(dst_db)

        src_run = src_adapter.get_run("run-001")
        dst_run = dst_adapter.get_run("run-001")
        assert src_run["id"] == dst_run["id"]
        assert src_run["agent_name"] == dst_run["agent_name"]
        assert src_run["status"] == dst_run["status"]

        src_steps = src_adapter.get_steps_with_messages("run-001")
        dst_steps = dst_adapter.get_steps_with_messages("run-001")
        assert len(src_steps) == len(dst_steps)

        src_adapter.close()
        dst_adapter.close()
        Path(out).unlink(missing_ok=True)
    finally:
        _cleanup(src_db)
        _cleanup(dst_db)


def test_import_batch():
    src_db = _seed_db(_tmp_db())
    dst_db = _tmp_db()
    try:
        exporter = TraceExporter(src_db)
        out_dir = str(Path(tempfile.gettempdir()) / f"batch_{time.time_ns()}")
        exporter.export_batch(output_dir=out_dir)

        importer = TraceImporter(dst_db)
        result = importer.import_batch(out_dir)
        assert result["imported"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == 0

        adapter = SQLiteAdapter(dst_db)
        run = adapter.get_run("run-001")
        assert run is not None
        adapter.close()

        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        _cleanup(src_db)
        _cleanup(dst_db)


if __name__ == "__main__":
    import traceback
    tests = [
        test_export_single_run,
        test_import_single_run,
        test_import_skip_existing,
        test_import_replace_existing,
        test_import_error_on_conflict,
        test_export_import_roundtrip,
        test_import_batch,
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

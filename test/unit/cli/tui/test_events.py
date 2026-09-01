# test/unit/cli/tui/test_events.py
from dataclasses import fields
from cli.tui.events import (
    TUIEvent,
    TimelineStepEvent,
    MetricEvent,
    TaskStartEvent,
    TaskEndEvent,
    ReportChunkEvent,
    ConfirmRequestEvent,
    ErrorEvent,
    UnlockInputEvent,
)


def test_timeline_step_event_fields():
    f = {x.name for x in fields(TimelineStepEvent)}
    assert f == {"step", "status", "elapsed", "duration_ms", "detail"}


def test_metric_event_fields():
    f = {x.name for x in fields(MetricEvent)}
    assert f == {"name", "value"}


def test_task_start_event_fields():
    f = {x.name for x in fields(TaskStartEvent)}
    assert f == {"task_id", "mode", "question"}


def test_task_end_event_fields():
    f = {x.name for x in fields(TaskEndEvent)}
    assert f == {"task_id", "status", "duration", "metrics"}


def test_report_chunk_event_fields():
    f = {x.name for x in fields(ReportChunkEvent)}
    assert f == {"content", "final"}


def test_confirm_request_event_fields():
    f = {x.name for x in fields(ConfirmRequestEvent)}
    assert "title" in f and "summary" in f and "diff" in f
    assert "backup_path" in f and "on_apply" in f and "on_cancel" in f


def test_error_event_has_recoverable_flag():
    e = ErrorEvent(message="x")
    assert e.recoverable is True


def test_unlock_input_event_is_subclass_of_tui_event():
    assert issubclass(UnlockInputEvent, TUIEvent)


def test_progress_event_alias_exists():
    """旧 ProgressEvent 仍可导入（向后兼容）。"""
    from cli.tui.events import ProgressEvent
    assert ProgressEvent is not None


def test_progress_event_default_fields():
    from cli.tui.events import ProgressEvent
    p = ProgressEvent()
    assert hasattr(p, "step")
    assert hasattr(p, "status")
    assert hasattr(p, "elapsed")
    assert hasattr(p, "description")
    assert hasattr(p, "task_id")

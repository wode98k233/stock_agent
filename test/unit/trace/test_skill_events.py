"""技能事件写入 API：落 step_type='skill'、无活动 run 时跳过。"""
import json

from utils.agent_trace.core import TraceRecorder


def _recorder(tmp_path) -> TraceRecorder:
    return TraceRecorder(agent_name="test", db_path=str(tmp_path / "trace.db"))


def _step_extra(rec: TraceRecorder, sid: int) -> dict:
    row = rec._get_conn().execute(
        "SELECT extra FROM steps WHERE id=?", (sid,)
    ).fetchone()
    return json.loads(row["extra"])


def test_record_skill_event_persists(tmp_path):
    rec = _recorder(tmp_path)
    try:
        run_id = rec.start_run("测试")
        sid = rec.record_skill_event(
            "select", {"selected": ["mx_data"], "reason": "需要行情"}
        )
        assert sid
        row = rec._get_conn().execute(
            "SELECT run_id, step_type, step_name, status, extra FROM steps WHERE id=?",
            (sid,),
        ).fetchone()
        assert row["run_id"] == run_id
        assert row["step_type"] == "skill"
        assert row["step_name"] == "select"
        assert row["status"] == "success"
        extra = json.loads(row["extra"])
        assert extra["kind"] == "select"
        assert extra["selected"] == ["mx_data"]
    finally:
        rec.close()


def test_record_skill_event_without_run_is_noop(tmp_path):
    rec = _recorder(tmp_path)
    try:
        assert rec.record_skill_event("select", {"selected": []}) is None
    finally:
        rec.close()


def test_record_skill_event_unknown_kind_label_falls_back(tmp_path):
    rec = _recorder(tmp_path)
    try:
        rec.start_run("测试")
        sid = rec.record_skill_event("custom_kind", {})
        assert sid
        extra = _step_extra(rec, sid)
        assert extra["kind"] == "custom_kind"
        assert extra["label"] == "custom_kind"
    finally:
        rec.close()


def test_record_skill_event_non_json_value_is_stringified(tmp_path):
    rec = _recorder(tmp_path)
    try:
        rec.start_run("测试")
        sid = rec.record_skill_event("select", {"tags": {"a", "b"}})
        assert sid
        extra = _step_extra(rec, sid)
        assert isinstance(extra["tags"], str)
        assert "a" in extra["tags"]
    finally:
        rec.close()


def test_record_skill_event_circular_payload_does_not_raise(tmp_path):
    rec = _recorder(tmp_path)
    try:
        rec.start_run("测试")
        data = {}
        data["self"] = data
        sid = rec.record_skill_event("select", data)
        assert sid
        extra = _step_extra(rec, sid)
        assert isinstance(extra["_raw"], str)
    finally:
        rec.close()


def test_record_skill_event_payload_cannot_override_kind(tmp_path):
    rec = _recorder(tmp_path)
    try:
        rec.start_run("测试")
        sid = rec.record_skill_event(
            "select", {"kind": "HACKED", "label": "hacked"}
        )
        assert sid
        row = rec._get_conn().execute(
            "SELECT step_name, output, extra FROM steps WHERE id=?", (sid,)
        ).fetchone()
        extra = json.loads(row["extra"])
        output = json.loads(row["output"])
        assert row["step_name"] == "select"
        assert extra["kind"] == "select"
        assert extra["label"] == "技能选择"
        assert output["kind"] == "select"
        assert output["label"] == "技能选择"
    finally:
        rec.close()


def test_record_skill_event_list_payload_does_not_raise(tmp_path):
    rec = _recorder(tmp_path)
    try:
        rec.start_run("测试")
        sid = rec.record_skill_event("select", ["mx_data", "mx_news"])
        assert sid
        extra = _step_extra(rec, sid)
        assert extra["kind"] == "select"
        assert extra["_raw"] == str(["mx_data", "mx_news"])
    finally:
        rec.close()


def test_record_skill_event_str_payload_does_not_raise(tmp_path):
    rec = _recorder(tmp_path)
    try:
        rec.start_run("测试")
        sid = rec.record_skill_event("select", "raw text")
        assert sid
        extra = _step_extra(rec, sid)
        assert extra["kind"] == "select"
        assert extra["_raw"] == "raw text"
    finally:
        rec.close()

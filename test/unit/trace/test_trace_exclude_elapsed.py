"""TraceRecorder 澄清相关：等待不计入 run/step 耗时，另记一步 clarify 交互。"""
import json
import time

from utils.agent_trace.core import TraceRecorder


def _recorder() -> TraceRecorder:
    return TraceRecorder(db_path=":memory:")


def test_exclude_elapsed_shifts_open_step_and_run():
    rec = _recorder()
    try:
        rec.start_run("分析茅台")
        rec._create_step("r1", None, "chain", "问题分类", {})
        before = rec._t["r1"]

        time.sleep(0.02)
        rec.exclude_elapsed(10)  # 模拟 10s 澄清等待

        assert rec._t["r1"] == before + 10
        assert rec._excluded_ms == 10000
        assert "r1" in rec._open_steps

        # run 耗时同样扣掉（不可能出现负值）
        rec.exclude_elapsed(10 ** 6)
        duration = (time.perf_counter() - rec._run_t0) * 1000 - rec._excluded_ms
        assert duration < 0  # 计算口径为负 → end_run 会 clamp 到 0
    finally:
        rec.close()


def test_exclude_elapsed_skips_finished_step():
    rec = _recorder()
    try:
        rec.start_run("x")
        rec._create_step("r1", None, "chain", "node", {})
        rec._finish_step("r1")
        assert "r1" not in rec._open_steps

        before = rec._t["r1"]
        rec.exclude_elapsed(5)
        # 已结束的 step 起点不再被平移
        assert rec._t["r1"] == before
    finally:
        rec.close()


def test_exclude_elapsed_ignores_bad_input():
    rec = _recorder()
    try:
        rec.start_run("x")
        rec.exclude_elapsed("junk")
        rec.exclude_elapsed(-3)
        assert rec._excluded_ms == 0
    finally:
        rec.close()


def test_record_clarify_writes_step_with_wait_s():
    """澄清交互单独落一步：耗时已剔除（duration_ms=0），真实等待在 extra.wait_s。"""
    rec = _recorder()
    try:
        rec.start_run("分析茅台")
        sid = rec.record_clarify(wait_s=300, question_count=3, answered=1,
                                 skipped=1, timed_out=True)
        assert sid

        row = rec._get_conn().execute(
            "SELECT step_type, step_name, status, duration_ms, extra "
            "FROM steps WHERE id=?", (sid,)
        ).fetchone()
        assert row["step_type"] == "clarify"
        assert row["step_name"] == "澄清反问"
        assert row["status"] == "success"
        assert row["duration_ms"] == 0
        extra = json.loads(row["extra"])
        assert extra["wait_s"] == 300
        assert extra["timed_out"] is True
        assert extra["question_count"] == 3
        assert extra["answered"] == 1 and extra["skipped"] == 1
        assert extra["outcome"] == "timeout"
    finally:
        rec.close()


def test_record_clarify_without_run_is_noop():
    rec = _recorder()
    try:
        assert rec.record_clarify(wait_s=10) is None
    finally:
        rec.close()

# -*- coding: utf-8 -*-
"""transcript 条目构造器单测：摘要截断 / entry_id 幂等 / 快照与增量同构。

背景：对话过程流（docs/superpowers/specs/2026-10-10-agent-transcript-ui-design.md），
SSE 增量事件与 GET /api/tasks/{id}/transcript 快照共用本模块构造逻辑。
"""
import sqlite3

import pytest

from server.runtime import transcript


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE steps (id INTEGER PRIMARY KEY, run_id TEXT, step_type TEXT,"
        " step_name TEXT, input TEXT, output TEXT, status TEXT, duration_ms INTEGER, error TEXT)"
    )
    # ⚠️ 必须与真实 schema 对齐（role/content/tool_calls 都在）：read_run_entries 会
    # SELECT m.content 并按 role='ai' 过滤，缺列直接 OperationalError。
    c.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, step_id INTEGER, seq INTEGER,"
        " role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT, reasoning TEXT)"
    )
    yield c
    c.close()


def _insert_msg(c, step_id, reasoning="", content=None, role="ai"):
    """插入一条 messages 行（默认 role=ai，与真实写入口径一致）。"""
    return c.execute(
        "INSERT INTO messages(step_id,seq,role,content,reasoning) VALUES(?,?,?,?,?)",
        (step_id, step_id, role, content, reasoning),
    ).lastrowid


def _insert_step(c, **kw):
    defaults = dict(run_id="run-1", step_type="tool", step_name="mx_data_query",
                    input=None, output=None, status="running", duration_ms=None, error=None)
    defaults.update(kw)
    cur = c.execute(
        "INSERT INTO steps(run_id,step_type,step_name,input,output,status,duration_ms,error) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (defaults["run_id"], defaults["step_type"], defaults["step_name"],
         defaults["input"], defaults["output"], defaults["status"],
         defaults["duration_ms"], defaults["error"]),
    )
    return cur.lastrowid


class TestSummarize:
    def test_none_and_json(self):
        assert transcript.summarize(None, 10) == ""
        assert transcript.summarize({"a": 1}, 100) == '{"a": 1}'

    def test_truncate_with_ellipsis(self):
        s = transcript.summarize("字" * 50, 10)
        assert s == "字" * 10 + "…"
        assert len(s) == 11

    def test_whitespace_collapsed(self):
        assert transcript.summarize("a\n  b\t c ", 100) == "a b c"


class TestBuildEntries:
    def test_tool_entry_shape_and_status_map(self, conn):
        sid = _insert_step(conn, input='{"q": 1}', output="x" * 400,
                           status="success", duration_ms=1200)
        row = dict(conn.execute("SELECT * FROM steps WHERE id=?", (sid,)).fetchone())
        e = transcript.build_tool_entry(row)
        assert e["entry_id"] == f"u:{sid}"
        assert e["kind"] == "tool"
        assert e["status"] == "ok"
        assert e["duration_ms"] == 1200
        assert e["output_summary"].endswith("…")
        assert len(e["output_summary"]) == transcript.OUTPUT_SUMMARY_CHARS + 1
        assert e["sort"] == [sid, 0]

    def test_tool_entry_status_error_and_running(self, conn):
        sid = _insert_step(conn, status="error", error="接口超时")
        row = dict(conn.execute("SELECT * FROM steps WHERE id=?", (sid,)).fetchone())
        assert transcript.build_tool_entry(row)["status"] == "error"
        sid2 = _insert_step(conn, status="running")
        row2 = dict(conn.execute("SELECT * FROM steps WHERE id=?", (sid2,)).fetchone())
        assert transcript.build_tool_entry(row2)["status"] == "running"

    def test_non_tool_step_ignored(self, conn):
        sid = _insert_step(conn, step_type="llm")
        row = dict(conn.execute("SELECT * FROM steps WHERE id=?", (sid,)).fetchone())
        assert transcript.build_tool_entry(row) is None

    def test_think_entry_requires_reasoning(self):
        assert transcript.build_think_entry({"id": 1, "step_id": 2, "reasoning": "  "}) is None
        e = transcript.build_think_entry({"id": 7, "step_id": 3, "reasoning": "想一想", "run_id": "r"})
        assert e["entry_id"] == "t:7"
        assert e["kind"] == "think"
        assert e["text"] == "想一想"
        assert e["sort"] == [3, 7]

    def test_think_entry_carries_content(self):
        """「思考+文字」轮：正文必须一起进条目（原来整段丢弃）。"""
        e = transcript.build_think_entry(
            {"id": 8, "step_id": 3, "reasoning": "想一想", "content": '{"ok":1}'})
        assert e["text"] == "想一想"
        assert e["content"] == '{"ok":1}'

    def test_think_entry_content_only(self):
        """纯正文轮（reasoning 空但有 content）不能被丢弃。"""
        e = transcript.build_think_entry({"id": 9, "step_id": 3, "reasoning": "", "content": "结论"})
        assert e is not None and e["text"] == "" and e["content"] == "结论"

    def test_think_entry_drops_compaction_summary(self):
        assert transcript.build_think_entry({
            "id": 10, "step_id": 3, "reasoning": "",
            "content": transcript.COMPACTION_PREFIX + " #1】R1..R2：\n```json\n{}\n```",
        }) is None

    def test_think_entry_truncates_long_content(self):
        e = transcript.build_think_entry({
            "id": 11, "step_id": 3, "reasoning": "r", "content": "字" * 9999})
        assert len(e["content"]) == transcript.CONTENT_CHARS
        assert e["content_truncated"] is True


class TestRunEntries:
    def test_snapshot_sorted_and_filtered(self, conn):
        sid_a = _insert_step(conn, input="A")
        _insert_msg(conn, sid_a, "推理A")
        sid_b = _insert_step(conn, input="B", status="success", output="outB")
        _insert_msg(conn, sid_b, "")
        # 另一个 run 不掺入
        _insert_step(conn, run_id="run-2")
        entries = transcript.read_run_entries(conn, "run-1")
        ids = [e["entry_id"] for e in entries]
        assert ids == [f"u:{sid_a}", "t:1", f"u:{sid_b}"]   # 按 sort=[step_id, sub] 排序
        assert all(e["run_id"] == "run-1" or e["kind"] == "think" for e in entries)

    def test_non_ai_roles_excluded(self, conn):
        """role 过滤是硬约束：system/tool/human 同样有 content，不排除会混进过程流。"""
        sid = _insert_step(conn, step_type="llm")
        _insert_msg(conn, sid, "", "系统提示词", role="system")
        _insert_msg(conn, sid, "", "用户问题", role="human")
        _insert_msg(conn, sid, "", "工具返回", role="tool")
        _insert_msg(conn, sid, "真的思考", '{"ok":1}')
        entries = transcript.read_run_entries(conn, "run-1")
        assert [e["entry_id"] for e in entries] == ["t:4"]
        assert entries[0]["text"] == "真的思考"

    def test_content_growth_triggers_version_bump(self, conn):
        """同 entry_id 正文增长必须能重推。"""
        sid = _insert_step(conn, step_type="llm")
        mid = _insert_msg(conn, sid, "思考", "短")
        seen: dict = {}
        assert len(transcript.changed_entries(conn, "run-1", seen)) == 1
        assert transcript.changed_entries(conn, "run-1", seen) == []
        conn.execute("UPDATE messages SET content='长很多' WHERE id=?", (mid,))
        again = transcript.changed_entries(conn, "run-1", seen)
        assert [e["entry_id"] for e in again] == [f"t:{mid}"]
        assert again[0]["content"] == "长很多"

    def test_changed_entries_idempotent_and_versioned(self, conn):
        sid = _insert_step(conn, input="A")           # running
        _insert_msg(conn, sid, "推理A")
        seen: dict = {}

        first = transcript.changed_entries(conn, "run-1", seen)
        assert {e["entry_id"] for e in first} == {f"u:{sid}", "t:1"}
        # 与快照同构（SSE 增量与快照必须一份逻辑）
        assert first == transcript.read_run_entries(conn, "run-1")
        # 无变化 → 不重推
        assert transcript.changed_entries(conn, "run-1", seen) == []

        # 状态推进 running→success：同一 entry_id 必须重推（否则前端停在 spinner）
        conn.execute("UPDATE steps SET status='success', output='done', duration_ms=900 WHERE id=?", (sid,))
        again = transcript.changed_entries(conn, "run-1", seen)
        assert [e["entry_id"] for e in again] == [f"u:{sid}"]
        assert again[0]["status"] == "ok"
        assert transcript.changed_entries(conn, "run-1", seen) == []

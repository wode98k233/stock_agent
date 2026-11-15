"""Transcript 条目构造器：AI 正文（messages.content）+ role 过滤。

背景（2026-10-15）：一条 AI 消息可能是「思考+工具调用」「思考+正文」或纯正文，
原实现只取 reasoning，把「思考+文字」轮次的正文整段丢弃（实测 1855/3275 条）。
新增 content 后引入两个必须守住的回归点：
  ① content 条件会把 system/tool/human 行也带进来（它们同样有 content）；
  ② 上下文压缩摘要是内部合成占位消息，不是模型真实输出。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from server.runtime import transcript  # noqa: E402


def _conn(rows):
    """内存 messages/steps，rows = [(msg_id, role, reasoning, content)]"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE steps (id INTEGER PRIMARY KEY, run_id TEXT, step_type TEXT,"
        " step_name TEXT, input TEXT, output TEXT, status TEXT, duration_ms REAL,"
        " error TEXT, extra TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, step_id INTEGER, seq INTEGER,"
        " role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT,"
        " reasoning TEXT)"
    )
    for mid, role, reasoning, content in rows:
        conn.execute(
            "INSERT INTO messages(id,step_id,seq,role,content,reasoning) VALUES(?,?,?,?,?,?)",
            (mid, 1, mid, role, content, reasoning),
        )
    conn.execute("INSERT INTO steps(id,run_id,step_type) VALUES(1,'r1','llm')")
    conn.commit()
    return conn


def test_think_entry_carries_both_reasoning_and_content():
    e = transcript.build_think_entry(
        {"id": 7, "step_id": 1, "reasoning": "想一下", "content": '{"a":1}'}
    )
    assert e["entry_id"] == "t:7"
    assert e["text"] == "想一下"
    assert e["content"] == '{"a":1}'


def test_content_only_message_is_not_dropped():
    """思考+文字轮：reasoning 为空但有正文 → 必须成条目（原来直接被丢弃）。"""
    e = transcript.build_think_entry(
        {"id": 8, "step_id": 1, "reasoning": "", "content": "结论如下"}
    )
    assert e is not None
    assert e["text"] == ""
    assert e["content"] == "结论如下"


def test_both_empty_returns_none():
    assert transcript.build_think_entry({"id": 9, "step_id": 1, "reasoning": " ", "content": None}) is None


def test_compaction_summary_is_excluded():
    """上下文压缩摘要是内部合成占位消息，不进过程流。"""
    e = transcript.build_think_entry({
        "id": 10, "step_id": 1, "reasoning": "",
        "content": "【已压缩历史轮次摘要 #1】R1..R2：\n```json\n{}\n```",
    })
    assert e is None


def test_content_is_truncated_with_flag():
    long = "字" * (transcript.CONTENT_CHARS + 500)
    e = transcript.build_think_entry({"id": 11, "step_id": 1, "reasoning": "r", "content": long})
    assert len(e["content"]) == transcript.CONTENT_CHARS
    assert e["content_truncated"] is True


def test_non_ai_roles_are_never_selected():
    """role 过滤是硬约束：system/tool/human 行同样有 content，不加会多带进数千行。"""
    conn = _conn([
        (1, "system", "", "你是一个助手"),
        (2, "human", "", "用户问题"),
        (3, "tool", "", "工具返回结果"),
        (4, "ai", "思考中", '{"ok":1}'),
    ])
    entries = transcript.read_run_entries(conn, "r1")
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "t:4"
    assert entries[0]["content"] == '{"ok":1}'


def test_entry_version_changes_when_content_grows():
    """正文增长必须能触发重推，否则同一 entry_id 的新正文永远推不出去。"""
    a = transcript.build_think_entry({"id": 12, "step_id": 1, "reasoning": "r", "content": "短"})
    b = transcript.build_think_entry({"id": 12, "step_id": 1, "reasoning": "r", "content": "长一点"})
    assert transcript._entry_version(a) != transcript._entry_version(b)
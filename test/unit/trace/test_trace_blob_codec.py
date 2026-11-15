"""内容寻址压缩（T2）的编解码与透明读。

设计：``docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md`` §7

四个必须守住的点：
1. 长文本往返**逐字节一致**；短文本内联（不额外查表）。
2. **老格式天然兼容**：没有哨兵前缀的列值原样返回，不需要全量迁移。
3. 装上行工厂后，`dict(row)` / `row["col"]` / 位置索引三种读法都能拿到原文。
4. `runs.input/output` **绝不压缩**：`store.search_runs` 用 SQL `LIKE` 做模糊搜索，
   压缩后 LIKE 必然失效（这是本设计的硬边界之一）。
"""
import json
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

_project_root = str(Path(__file__).parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.agent_trace import blobstore


def _tmp_db():
    return Path(tempfile.gettempdir()) / f"test_blob_{uuid.uuid4().hex[:8]}.db"


def _cleanup(db_path: Path):
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except PermissionError:
            pass


def _schema_conn(db_path: Path) -> sqlite3.Connection:
    from utils.agent_trace.models import ensure_full_schema

    conn = sqlite3.connect(str(db_path))
    ensure_full_schema(conn)
    conn.commit()
    return conn


# ============================================================
# 编解码
# ============================================================

def test_long_text_roundtrip_and_short_text_inlined():
    db_path = _tmp_db()
    try:
        conn = _schema_conn(db_path)
        long_text = "上证指数 3730.92 收盘点评 " * 200  # ~6KB
        ref = blobstore.encode_text(conn, long_text)
        assert blobstore.is_ref(ref) and len(ref) < 64
        assert blobstore.decode_text(conn, ref) == long_text

        short = "短文本"
        assert blobstore.encode_text(conn, short) == short
        assert blobstore.decode_text(conn, short) == short
        conn.close()
    finally:
        _cleanup(db_path)


def test_legacy_plain_text_passes_through():
    db_path = _tmp_db()
    try:
        conn = _schema_conn(db_path)
        legacy = '[\n  [\n    "content=\'x\'"\n  ]\n]'
        assert blobstore.decode_text(conn, legacy) == legacy
        assert blobstore.decode_text(conn, None) is None
        assert blobstore.decode_text(conn, 42) == 42
        # 形似哨兵但解析不出合法 hash → 原样返回，不炸
        assert blobstore.decode_text(conn, "\x01Bzlib") == "\x01Bzlib"
        conn.close()
    finally:
        _cleanup(db_path)


def test_identical_text_stored_once():
    """内容寻址：同一段文本写两次只占一条 blob。"""
    db_path = _tmp_db()
    try:
        conn = _schema_conn(db_path)
        text = "同一段工具输出 " * 500
        ref1 = blobstore.encode_text(conn, text)
        ref2 = blobstore.encode_text(conn, text)
        conn.commit()
        assert ref1 == ref2
        assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        # 压缩比：中文 + JSON 结构，实测约 3x
        raw, packed = conn.execute("SELECT sum(raw_len), sum(length(data)) FROM blobs").fetchone()
        assert packed < raw / 2
        conn.close()
    finally:
        _cleanup(db_path)


def test_missing_blob_does_not_crash():
    db_path = _tmp_db()
    try:
        conn = _schema_conn(db_path)
        orphan = "\x01Bzlib:" + "0" * 32
        assert blobstore.decode_text(conn, orphan) == orphan  # 原样暴露，不抛
        conn.close()
    finally:
        _cleanup(db_path)


# ============================================================
# 行工厂：所有读法都该拿到原文
# ============================================================

def test_row_factory_decodes_and_supports_all_access_styles():
    db_path = _tmp_db()
    try:
        conn = _schema_conn(db_path)
        text = "很长的消息内容 " * 300
        ref = blobstore.encode_text(conn, text)
        conn.execute("CREATE TABLE demo(a TEXT, b TEXT)")
        conn.execute("INSERT INTO demo VALUES(?,?)", (ref, "短值"))
        conn.commit()
        conn.close()

        raw = sqlite3.connect(str(db_path))
        stored = raw.execute("SELECT a FROM demo").fetchone()[0]
        assert blobstore.is_ref(stored), "写入端没真的压缩"
        raw.close()

        conn = sqlite3.connect(str(db_path))
        blobstore.install_row_factory(conn)
        row = conn.execute("SELECT a, b FROM demo").fetchone()
        assert row["a"] == text          # 按列名
        assert row[0] == text           # 按位置
        assert dict(row)["a"] == text   # dict() 协议
        assert row["b"] == "短值"
        assert list(row.keys()) == ["a", "b"]
        # 混合取值（同级多行）也要各自正确
        conn.execute("INSERT INTO demo VALUES(?,?)", (ref, "另一行"))
        conn.commit()
        rows = [dict(r) for r in conn.execute("SELECT a, b FROM demo").fetchall()]
        assert [r["a"] for r in rows] == [text, text]
        conn.close()
    finally:
        _cleanup(db_path)


# ============================================================
# 与写入端集成
# ============================================================

def test_recorder_compresses_steps_and_messages_but_not_runs():
    from utils.agent_trace import TraceRecorder

    db_path = _tmp_db()
    try:
        r = TraceRecorder("test_agent", str(db_path))
        root = r.start_run("Q" * 3000)  # runs.input 长文本：**必须保持明文**
        prompts = [SimpleNamespace(type="system", content="S" * 20000,
                                   tool_calls=None, tool_call_id=None)]
        r.on_llm_start({"name": "m", "kwargs": {"model_name": "m"}},
                       [prompts], run_id="llm-1", parent_run_id=root)
        r.on_chain_start({"name": "big_node"}, {"messages": [{"c": "M" * 20000}]},
                         run_id="node-1", parent_run_id=root)
        r.on_chain_end({"blob": "O" * 20000}, run_id="node-1")
        r.close()

        raw = sqlite3.connect(str(db_path))
        run_input = raw.execute("SELECT input FROM runs").fetchone()[0]
        assert not blobstore.is_ref(run_input), "runs.input 被压缩了 → LIKE 搜索会失效"
        assert len(run_input) >= 3000

        msg = raw.execute("SELECT content FROM messages").fetchone()[0]
        assert blobstore.is_ref(msg), "messages.content 没有压缩"
        step_in = raw.execute("SELECT input FROM steps WHERE event_run_id='node-1'").fetchone()[0]
        assert blobstore.is_ref(step_in) or len(step_in) < 4096  # digest 后再压缩
        raw.close()

        # 读侧：装上工厂后一切照旧
        conn = sqlite3.connect(str(db_path))
        blobstore.install_row_factory(conn)
        content = conn.execute("SELECT content FROM messages").fetchone()["content"]
        assert content == "S" * 20000
        out = conn.execute("SELECT output FROM steps WHERE event_run_id='node-1'").fetchone()["output"]
        assert json.loads(out)["_kind"] == "state_digest"
        conn.close()
    finally:
        _cleanup(db_path)

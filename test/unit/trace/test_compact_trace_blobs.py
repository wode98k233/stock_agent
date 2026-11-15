"""存量回填脚本的行为护栏（`scripts/ops/compact_trace_blobs.py`）。

设计 §7.3。这个脚本会改写历史数据，所以四条性质必须有测试盯着：

1. **预演不动数据**：`preview_db` 对只读连接工作，跑完文件字节数不变；
2. **回填不丢行**：`runs/steps/messages` 行数前后一致，`messages.content` 解码后逐字一致；
3. **幂等**：连跑两次，第二次不再产生新 blob、不再改写行、体积不再变化；
   （幂等是"跑一半崩了能不能重来"的前提）
4. **备份是真备份**：用 SQLite 在线备份 API，必须能完整读出（含 WAL 里已提交的事务）。
   —— 这条是为 `shutil.copy2` 那次事故设的护栏：演练时 4795 步的库只拷出 4790 步。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

_SCRIPT = _project_root / "scripts" / "ops" / "compact_trace_blobs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("compact_trace_blobs_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def legacy_db(tmp_path):
    """造一个"改动前格式"的月库：steps 里全是整份 prompt/state，messages 全是明文。"""
    from utils.agent_trace import TraceRecorder

    db_path = tmp_path / "agent_trace_202601.db"
    recorder = TraceRecorder("legacy_agent", str(db_path))
    run_id = recorder.start_run("q")

    # 老格式：llm 步的 input 是 prompt 的 repr（不是摘要）；节点步是整份 state；没有 blob
    big_state = {
        "messages": [{"role": "human", "content": "M" * 4000} for _ in range(5)],
        "tool_calls": [{"name": "mx_data_query", "result": "R" * 8000} for _ in range(3)],
        "dispatch_history": [{"round": i, "detail": "D" * 500} for i in range(8)],
        "current_step_index": 1,
        "selected_template_id": "market_daily",
    }
    conn = recorder._get_conn()
    legacy_prompt = json.dumps([["content='%s'" % ("S" * 12000)]], ensure_ascii=False)
    rows = [
        (run_id, "legacy-llm", "llm", legacy_prompt, None, "success"),
        (run_id, "legacy-node", "agent", json.dumps(big_state, ensure_ascii=False),
         json.dumps(big_state, ensure_ascii=False), "success"),
        (run_id, "legacy-tool", "tool", json.dumps({"query": "上证指数"}, ensure_ascii=False),
         json.dumps({"rows": [{"v": "T" * 5000}]}, ensure_ascii=False), "success"),
    ]
    for rid, ev, stype, inp, out, status in rows:
        conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,output,status,started_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (rid, ev, stype, ev, inp, out, status, "2026-01-05T00:00:00"),
        )
    step_id = conn.execute("SELECT id FROM steps WHERE event_run_id='legacy-llm'").fetchone()[0]
    conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,reasoning) VALUES(?,0,'system',?,NULL)",
        (step_id, "P" * 9000),
    )
    conn.commit()
    recorder.close()

    from utils.agent_trace import blobstore

    blobstore.clear_cache()
    yield db_path, run_id
    blobstore.clear_cache()


def _row_counts(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("runs", "steps", "messages")}
    finally:
        conn.close()


def _content_of(path: Path, mid: int):
    from utils.agent_trace import blobstore

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        blobstore.install_row_factory(conn)
        return conn.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()[0]
    finally:
        conn.close()


def test_preview_is_read_only(legacy_db):
    module = _load_script()
    db_path, _ = legacy_db
    before_bytes = db_path.stat().st_size
    before_rows = _row_counts(db_path)

    info = module.preview_db(db_path)

    assert info["digest_rows"] > 0, "老格式的整份 state 应该被识别为可摘要"
    assert info["blob_rows"] > 0, "长文本应该被识别为可压缩"
    assert info["projected"] < info["before"]
    assert db_path.stat().st_size == before_bytes, "预演不许写盘"
    assert _row_counts(db_path) == before_rows


def test_apply_preserves_facts_and_is_idempotent(legacy_db):
    module = _load_script()
    db_path, run_id = legacy_db

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    mid = conn.execute("SELECT id FROM messages LIMIT 1").fetchone()[0]
    conn.close()
    original_content = _content_of(db_path, mid)
    before_rows = _row_counts(db_path)
    before_bytes = db_path.stat().st_size

    first = module._apply_db(db_path)
    after_first_bytes = db_path.stat().st_size

    # ① 行数不变（一条都不许丢）
    assert _row_counts(db_path) == before_rows
    # ② 事实逐字保留（压缩后再解压必须完全相同）
    assert _content_of(db_path, mid) == original_content == "P" * 9000
    # ③ 真的变小了，并且比预估还小（预估没把"同内容只存一份"算进去）
    assert after_first_bytes < first["before"]
    assert after_first_bytes <= first["before"] / 2

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    blobs_first = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    conn.close()

    # ④ 幂等：再跑一次不该再有可做的事
    second_preview = module.preview_db(db_path)
    assert second_preview["digest_rows"] == 0
    assert second_preview["blob_rows"] == 0

    second = module._apply_db(db_path)
    assert db_path.stat().st_size <= after_first_bytes * 1.05  # VACUUM 抖动余量
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == blobs_first
    conn.close()
    assert _content_of(db_path, mid) == original_content
    assert first["backup"] != second["backup"], "每次 apply 都该留独立备份"


def test_backup_is_readable_and_complete(legacy_db):
    """护栏：备份必须能完整读出（含 WAL 里已提交的事务）。"""
    module = _load_script()
    db_path, _ = legacy_db
    before_rows = _row_counts(db_path)

    backup = module._backup(db_path)

    assert Path(backup).exists() and Path(backup).stat().st_size > 0
    assert _row_counts(Path(backup)) == before_rows


def test_tool_step_evidence_survives_backfill(legacy_db):
    """tool 步的入参/出参是证据原文：阶段 A 不许动，阶段 B 压缩后解码仍逐字一致。"""
    from utils.agent_trace import blobstore

    module = _load_script()
    db_path, _ = legacy_db
    module._apply_db(db_path)

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    blobstore.install_row_factory(conn)
    row = conn.execute(
        "SELECT input, output FROM steps WHERE event_run_id='legacy-tool'"
    ).fetchone()
    conn.close()
    assert json.loads(row["input"]) == {"query": "上证指数"}
    assert json.loads(row["output"])["rows"][0]["v"] == "T" * 5000


def test_anchor_is_project_root():
    """scripts/ 约定：根锚点统一 `parents[2]`，别在别处另起一套。"""
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "parents[2]" in text

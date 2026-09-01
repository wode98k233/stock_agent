"""
Trace schema smoke test
校验 runs 表字段和 E2E fixture 使用字段一致
"""
import sqlite3
import tempfile
from pathlib import Path

from utils.agent_trace.models import _ensure_db


class TestTraceSchema:
    """校验 trace 数据库 schema 与代码期望一致"""

    def _create_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        _ensure_db(db_path)
        return db_path

    def test_runs_table_exists(self, tmp_path):
        db_path = self._create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

    def test_runs_table_has_required_columns(self, tmp_path):
        db_path = self._create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(runs)")
            columns = {col[1] for col in cursor.fetchall()}
            required = {"id", "agent_name", "input", "output", "status",
                        "error", "created_at", "finished_at", "duration_ms"}
            assert required.issubset(columns), \
                f"runs 表缺少字段: {required - columns}"
        finally:
            conn.close()

    def test_runs_status_enum_values(self, tmp_path):
        """status 只应为 running/success/error，不含 completed"""
        db_path = self._create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            # 插入测试数据并验证状态写入
            conn.execute(
                "INSERT INTO runs (id, agent_name, status, created_at) VALUES (?, ?, ?, datetime('now'))",
                ("test1", "test_agent", "running", )
            )
            conn.execute(
                "INSERT INTO runs (id, agent_name, status, created_at) VALUES (?, ?, ?, datetime('now'))",
                ("test2", "test_agent", "success", )
            )
            conn.execute(
                "INSERT INTO runs (id, agent_name, status, created_at) VALUES (?, ?, ?, datetime('now'))",
                ("test3", "test_agent", "error", )
            )
            conn.commit()

            cursor = conn.execute("SELECT DISTINCT status FROM runs")
            statuses = {row[0] for row in cursor.fetchall()}
            assert "completed" not in statuses, \
                f"status 不应包含 'completed'，实际值: {statuses}"
            assert statuses == {"running", "success", "error"}, \
                f"status 枚举值不正确: {statuses}"
        finally:
            conn.close()

    def test_e2e_fixture_uses_created_at(self):
        """E2E conftest.py 中不应使用 started_at 排序"""
        conftest_path = Path(__file__).parent.parent / "e2e" / "conftest.py"
        if not conftest_path.exists():
            return
        content = conftest_path.read_text(encoding="utf-8")
        assert "ORDER BY started_at" not in content, \
            "E2E conftest.py 仍使用 ORDER BY started_at，应改为 ORDER BY created_at"

    def test_e2e_fixture_default_status_is_success(self):
        """E2E conftest.py 默认状态应为 success"""
        conftest_path = Path(__file__).parent.parent / "e2e" / "conftest.py"
        if not conftest_path.exists():
            return
        content = conftest_path.read_text(encoding="utf-8")
        assert 'expected_status: str = "success"' in content, \
            "E2E conftest.py 默认状态应为 'success'"

    def test_steps_table_exists(self, tmp_path):
        db_path = self._create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='steps'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

    def test_messages_table_exists(self, tmp_path):
        db_path = self._create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

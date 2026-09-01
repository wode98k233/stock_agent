"""
P4: get_logger skip_db 参数测试

问题：bootstrap() 用 get_logger("system") 初始化，
导致每次启动都创建 user_query="system" 的垃圾对话记录和空日志。
修复：get_logger 增加 skip_db=True 参数。

运行方式：
  pytest test/unit/test_plan_steps/test_logger_skip_db.py -v
"""
import os
import sys
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def test_get_logger_skip_db_no_insert():
    """P4: get_logger("system", skip_db=True) 不应插入 dialog 表"""
    from utils.logger import get_logger, close_logger

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_dialog.db")
        conn = sqlite3.connect(db_path)
        conn.execute('''CREATE TABLE IF NOT EXISTS dialog (
            dialog_uuid TEXT, user_query TEXT, create_time TEXT, log_file TEXT
        )''')
        conn.commit()

        with patch("utils.logger.get_db") as mock_get_db, \
             patch("utils.logger.Config.get_log_dir", return_value=tmp_dir):
            mock_get_db.return_value = conn

            radar_logger, uuid_str, ctx, _ = get_logger("system", skip_db=True)
            close_logger(radar_logger)

        conn.commit()
        rows = conn.execute("SELECT * FROM dialog WHERE user_query='system'").fetchall()
        conn.close()

        assert len(rows) == 0, f"skip_db=True should not insert into dialog, found {len(rows)} rows"


def test_get_logger_normal_inserts():
    """正常调用 get_logger("用户问题") 应插入 dialog 表"""
    from utils.logger import get_logger, close_logger

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_dialog.db")
        conn = sqlite3.connect(db_path)
        conn.execute('''CREATE TABLE IF NOT EXISTS dialog (
            dialog_uuid TEXT, user_query TEXT, create_time TEXT, log_file TEXT
        )''')
        conn.commit()

        with patch("utils.logger.get_db") as mock_get_db, \
             patch("utils.logger.Config.get_log_dir", return_value=tmp_dir):
            mock_get_db.return_value = conn

            radar_logger, uuid_str, ctx, _ = get_logger("查询茅台股价")
            close_logger(radar_logger)

        conn.commit()
        rows = conn.execute("SELECT * FROM dialog WHERE user_query='查询茅台股价'").fetchall()
        conn.close()

        assert len(rows) == 1, f"normal call should insert 1 row, found {len(rows)} rows"


def test_get_logger_skip_db_no_log_file():
    """P4: skip_db=True 不应创建对话日志文件"""
    from utils.logger import get_logger, close_logger, RadarLogger

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_dialog.db")
        conn = sqlite3.connect(db_path)
        conn.execute('''CREATE TABLE IF NOT EXISTS dialog (
            dialog_uuid TEXT, user_query TEXT, create_time TEXT, log_file TEXT
        )''')
        conn.commit()

        with patch("utils.logger.get_db") as mock_get_db, \
             patch("utils.logger.Config.get_log_dir", return_value=tmp_dir):
            mock_get_db.return_value = conn

            radar_logger, uuid_str, ctx, _ = get_logger("system_init", skip_db=True)

            assert isinstance(radar_logger, RadarLogger), "should return RadarLogger"
            assert uuid_str, "should return a uuid"

            log_file = os.path.join(tmp_dir, f"{uuid_str}.log")
            assert not os.path.exists(log_file), "skip_db=True should NOT create per-dialog log file"

            close_logger(radar_logger)

        conn.close()


if __name__ == "__main__":
    import traceback
    tests = [
        test_get_logger_skip_db_no_insert,
        test_get_logger_normal_inserts,
        test_get_logger_skip_db_no_log_file,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)
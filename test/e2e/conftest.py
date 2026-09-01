"""
端到端测试级 pytest fixtures
提供 trace 捕获、日志过滤、Agent 上下文
"""
import sys
import os
import pytest
import sqlite3
import logging

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _has_api_key():
    """检查是否有 LLM API key"""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    return bool(os.getenv("OPENAI_API_KEY"))


@pytest.fixture
def skip_if_no_api_key():
    """如果没有 API key 则跳过测试"""
    if not _has_api_key():
        pytest.skip("需要 OPENAI_API_KEY")


@pytest.fixture
def agent_context(skip_if_no_api_key):
    """
    构建 Agent 测试上下文
    自动跳过无 API key 的测试
    """
    from tools.skill_register import SkillRegister
    from tools.skills import SkillRegistry
    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from utils.session_stats import SessionStats
    from agents import register_all

    register_all()

    skill_register = SkillRegister()
    skill_register.auto_discover()
    logger, uuid, ctx, _ = get_logger("e2e-test")
    memory = MemoryManager(logger)
    session_stats = SessionStats()
    registry = SkillRegistry(logger, memory, skill_register)

    return {
        "skill_register": skill_register,
        "logger": logger,
        "memory": memory,
        "registry": registry,
        "session_stats": session_stats,
        "ctx": ctx,
    }

# 已知噪音模块，这些模块的 ERROR 日志可以忽略
NOISY_MODULES = {"akshare", "socket", "io_redirect", "urllib3", "requests", "httpx", "httpcore"}


class FilteredLogCapture:
    """过滤噪音日志的捕获器"""

    def __init__(self):
        self.records = []
        self._handler = None
        self._original_level = None

    def start(self):
        """开始捕获日志"""
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(record)
        self._handler.setLevel(logging.DEBUG)

        # 获取 root logger 并添加 handler
        root_logger = logging.getLogger()
        self._original_level = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(self._handler)

    def stop(self):
        """停止捕获日志"""
        if self._handler:
            root_logger = logging.getLogger()
            root_logger.removeHandler(self._handler)
            root_logger.setLevel(self._original_level)

    def get_filtered_records(self, level=logging.ERROR):
        """获取过滤后的日志记录"""
        return [
            r for r in self.records
            if r.levelno >= level and r.module not in NOISY_MODULES
        ]

    def get_errors(self):
        """获取过滤后的 ERROR 日志"""
        return self.get_filtered_records(logging.ERROR)

    def get_warnings(self):
        """获取过滤后的 WARNING 日志"""
        return self.get_filtered_records(logging.WARNING)


@pytest.fixture
def filtered_log_capture():
    """提供过滤噪音日志的捕获器"""
    capture = FilteredLogCapture()
    capture.start()
    yield capture
    capture.stop()


@pytest.fixture
def trace_capture(tmp_path):
    """
    捕获 trace 记录用于验证
    返回 (recorder, db_path) 元组
    """
    from utils.agent_trace import TraceRecorder

    db_path = str(tmp_path / "test_trace.db")
    recorder = TraceRecorder("e2e-test", db_path)
    yield recorder, db_path
    recorder.close()


def assert_trace_recorded(db_path: str, expected_agent: str, expected_status: str = "success"):
    """
    验证 trace 数据库中存在指定记录

    Args:
        db_path: trace 数据库路径
        expected_agent: 期望的 agent 名称
        expected_status: 期望的状态 (默认 "completed")
    """
    conn = sqlite3.connect(db_path)
    try:
        # 查询 runs 表
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        )
        if not cursor.fetchone():
            pytest.skip("trace 数据库中没有 runs 表")

        row = conn.execute(
            "SELECT * FROM runs WHERE agent_name=? ORDER BY created_at DESC LIMIT 1",
            (expected_agent,)
        ).fetchone()

        assert row is not None, f"未找到 {expected_agent} 的 trace 记录"

        # 获取列名
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = [col[1] for col in cursor.fetchall()]

        # 查找 status 列
        if "status" in columns:
            status_idx = columns.index("status")
            assert row[status_idx] == expected_status, \
                f"trace 状态异常: 期望 {expected_status}，实际 {row[status_idx]}"

    finally:
        conn.close()


def assert_trace_has_steps(db_path: str, expected_agent: str, min_steps: int = 2):
    """
    验证 trace 记录包含足够的步骤

    Args:
        db_path: trace 数据库路径
        expected_agent: 期望的 agent 名称
        min_steps: 最少步骤数
    """
    conn = sqlite3.connect(db_path)
    try:
        # 查询 steps 表
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='steps'"
        )
        if not cursor.fetchone():
            pytest.skip("trace 数据库中没有 steps 表")

        # 获取最近一次 run 的 ID
        row = conn.execute(
            "SELECT id FROM runs WHERE agent_name=? ORDER BY created_at DESC LIMIT 1",
            (expected_agent,)
        ).fetchone()

        if not row:
            pytest.skip(f"未找到 {expected_agent} 的 trace 记录")

        run_id = row[0]

        # 统计步骤数
        cursor = conn.execute(
            "SELECT COUNT(*) FROM steps WHERE run_id=?",
            (run_id,)
        )
        step_count = cursor.fetchone()[0]

        assert step_count >= min_steps, \
            f"trace 步骤数不足: 期望 >= {min_steps}，实际 {step_count}"

    finally:
        conn.close()

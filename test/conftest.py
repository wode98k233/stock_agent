"""
项目级 pytest fixtures
自动处理 sys.path 和项目根目录
"""
import sys
import os
import pytest

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 确保项目根目录在 sys.path 中
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def project_root():
    """返回项目根目录路径"""
    return PROJECT_ROOT


@pytest.fixture
def tmp_trace_db(tmp_path):
    """提供临时 trace 数据库路径"""
    return str(tmp_path / "test_trace.db")


@pytest.fixture
def in_memory_db():
    """提供内存 SQLite 数据库连接"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()

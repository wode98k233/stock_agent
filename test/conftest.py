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

# ── 并行测试（pytest-xdist）日志隔离 ──────────────────────────────────────
# `utils/logger.py` 在 **import 期** 就会打开 logs/radar-system.log。用 `-n N` 时
# 多个 worker 同时打开同一个文件 → Windows PermissionError → worker 当场死 →
# pytest 报 "Different tests were collected between gw*" / collection error。
# 给每个 worker 一份独立文件即可根治（utils/logger 读 LOG_FILE_SUFFIX）。
# ⚠️ 必须放在本文件**顶层**：同级 conftest 与测试模块在它之后才被导入。
os.environ.setdefault(
    "LOG_FILE_SUFFIX",
    f".{os.environ.get('PYTEST_XDIST_WORKER') or 'serial'}")


@pytest.fixture(autouse=True, scope="session")
def _isolate_pool_study_output(tmp_path_factory):
    """测试期把池子研究落盘目录改到 tmp：单测里意外真起池子线程时，
    失败记录不会写进 output/pool_studies/（否则池子研究页会堆满「失败」残留，
    2026-10-25 用户看到 56 条全是这种）。"""
    try:
        from backtest import pool_study_store
        pool_study_store.STUDY_DIR = tmp_path_factory.mktemp("pool_studies")
    except Exception:  # noqa: BLE001 - 导入失败不影响其它用例
        pass
    yield


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

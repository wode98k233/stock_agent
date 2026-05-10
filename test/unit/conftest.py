"""
单元测试级 pytest fixtures
提供 mock 对象和通用测试工具
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def mock_llm():
    """Mock LLM 实例"""
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="测试响应")
    llm.ainvoke.return_value = MagicMock(content="测试响应")
    return llm


@pytest.fixture
def temp_dir(tmp_path):
    """自动清理的临时目录"""
    return tmp_path


@pytest.fixture
def mock_logger():
    """Mock logger 实例"""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.debug = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


@pytest.fixture
def mock_memory():
    """Mock memory 实例"""
    memory = MagicMock()
    memory.chat_memory = MagicMock()
    memory.chat_memory.messages = []
    memory.save_context = MagicMock()
    memory.load_memory_variables = MagicMock(return_value={"history": ""})
    return memory


@pytest.fixture
def mock_skill_registry():
    """Mock skill registry 实例"""
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    registry.execute_tool = MagicMock(return_value="工具执行结果")
    return registry


@pytest.fixture
def load_scenario_module():
    """
    加载 agents.scenarios 下的模块，避免 agents/__init__.py 重依赖
    用于 test_scenario_*.py 测试
    """
    import importlib.util

    def _load(module_name, relative_path):
        """
        加载指定模块

        Args:
            module_name: 模块名 (如 "common", "stock_analysis")
            relative_path: 相对于项目根目录的路径 (如 "agents/scenarios/common.py")
        """
        full_path = os.path.join(PROJECT_ROOT, relative_path)
        spec = importlib.util.spec_from_file_location(module_name, full_path)
        mod = importlib.util.module_from_spec(spec)

        # 预注册父包到 sys.modules 避免导入错误
        parts = relative_path.replace("/", ".").replace(".py", "").split(".")
        for i in range(len(parts) - 1):
            parent = ".".join(parts[:i+1])
            if parent not in sys.modules:
                sys.modules[parent] = MagicMock()

        spec.loader.exec_module(mod)
        return mod

    return _load


@pytest.fixture
def isolated_cache(tmp_path):
    """
    隔离的缓存数据库，避免测试间干扰
    """
    import sqlite3
    db_path = str(tmp_path / "test_cache.db")
    conn = sqlite3.connect(db_path)
    yield conn, db_path
    conn.close()

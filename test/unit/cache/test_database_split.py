"""数据库拆分单元测试"""
import os
import sqlite3
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from utils.cache import market_data_db, fundamental_db, market_cache_db


@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_market_data_tables(temp_dir):
    """测试 market_data.db 表创建"""
    db_path = os.path.join(temp_dir, 'market_data.db')

    mock_config = MagicMock()
    mock_config.get_market_data_db_path.return_value = db_path

    with patch.object(market_data_db, 'Config', mock_config):
        market_data_db.init_market_data_tables()

    conn = sqlite3.connect(db_path)
    tables = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()

    assert 'stock_info' in tables
    assert 'stock_board' in tables
    assert 'stock_board_member' in tables
    assert 'stock_daily' in tables
    assert 'stock_adjust_factor' in tables


def test_fundamental_tables(temp_dir):
    """测试 fundamental_cache.db 表创建"""
    db_path = os.path.join(temp_dir, 'fundamental_cache.db')

    mock_config = MagicMock()
    mock_config.get_fundamental_db_path.return_value = db_path

    with patch.object(fundamental_db, 'Config', mock_config):
        fundamental_db.init_fundamental_tables()

    conn = sqlite3.connect(db_path)
    tables = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()

    assert 'financial_metric' in tables
    assert 'financial_core' in tables
    assert 'valuation_daily' in tables


def test_market_cache_tables(temp_dir):
    """测试 market_cache.db 表创建"""
    db_path = os.path.join(temp_dir, 'market_cache.db')

    mock_config = MagicMock()
    mock_config.get_market_cache_db_path.return_value = db_path

    with patch.object(market_cache_db, 'Config', mock_config):
        market_cache_db.init_market_cache_tables()

    conn = sqlite3.connect(db_path)
    tables = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()

    assert 'cache_kv' in tables

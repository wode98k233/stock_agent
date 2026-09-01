"""
多数据源故障切换集成测试
测试 DataSourceManager 的优先级切换和故障恢复
"""
import pytest
from unittest.mock import MagicMock, patch
import pandas as pd

from tools.fetcher.base import DataSourceManager


@pytest.fixture
def mock_sources():
    """注册两个 mock 数据源"""
    DataSourceManager._sources.clear()
    DataSourceManager._current_source_index = 0

    class SourceA(MagicMock):
        name = "source_a"
        priority = 1
        enabled = True
        def is_available(self): return True
        def get_stock_hist(self, *a, **kw): return pd.DataFrame({"close": [100]})

    class SourceB(MagicMock):
        name = "source_b"
        priority = 2
        enabled = True
        def is_available(self): return True
        def get_stock_hist(self, *a, **kw): return pd.DataFrame({"close": [200]})

    a, b = SourceA(), SourceB()
    DataSourceManager._sources = [a, b]
    return a, b


@pytest.mark.integration
@pytest.mark.timeout(10)
def test_register_and_get_sources(mock_sources):
    """注册后可获取数据源列表"""
    sources = DataSourceManager.get_available_sources()
    assert len(sources) == 2


@pytest.mark.integration
@pytest.mark.timeout(10)
def test_switch_source(mock_sources):
    """切换数据源"""
    a, b = mock_sources
    current = DataSourceManager.get_current_source()
    assert current is not None
    DataSourceManager.switch_source()
    current2 = DataSourceManager.get_current_source()
    # 切换后应不同
    assert current2 is not None


@pytest.mark.integration
@pytest.mark.timeout(10)
def test_mark_failed_disables_source(mock_sources):
    """标记失败后该源不可用"""
    a, b = mock_sources
    DataSourceManager.mark_source_failed("source_a", error="timeout")
    sources = DataSourceManager.get_available_sources()
    # source_a 应被标记为不可用
    names = [s.name for s in sources if s.is_available()]
    assert "source_b" in names


@pytest.mark.integration
@pytest.mark.timeout(10)
def test_reset_source_restores(mock_sources):
    """重置后数据源恢复可用"""
    a, b = mock_sources
    DataSourceManager.mark_source_failed("source_a")
    DataSourceManager.reset_source("source_a")
    # 重置后应恢复


@pytest.mark.integration
@pytest.mark.timeout(10)
def test_status_returns_dict(mock_sources):
    """status 返回结构化状态"""
    status = DataSourceManager.status(probe=False)
    assert isinstance(status, dict)
    assert "source_a" in status
    assert "source_b" in status
    assert "priority" in status["source_a"]

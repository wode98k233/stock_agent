"""
测试目标: tools/search/factory.py
覆盖范围:
  - SearchProviderFactory._parse_keys: 空值/逗号分隔/空白过滤
  - SearchProviderFactory.create_providers: 各 provider 创建逻辑
Mock 策略: mock os.getenv、provider 构造函数
"""
import pytest
from unittest.mock import patch, MagicMock


class TestParseKeys:
    """_parse_keys: API Key 解析"""

    def test_empty_returns_empty(self):
        from tools.search.factory import SearchProviderFactory
        with patch.dict("os.environ", {"TEST_VAR": ""}, clear=False):
            assert SearchProviderFactory._parse_keys("TEST_VAR") == []

    def test_missing_var_returns_empty(self):
        from tools.search.factory import SearchProviderFactory
        with patch.dict("os.environ", {}, clear=False):
            # 删除不存在的变量
            import os
            os.environ.pop("NONEXISTENT_VAR", None)
            assert SearchProviderFactory._parse_keys("NONEXISTENT_VAR") == []

    def test_single_key(self):
        from tools.search.factory import SearchProviderFactory
        with patch.dict("os.environ", {"TEST_VAR": "sk-abc123"}, clear=False):
            result = SearchProviderFactory._parse_keys("TEST_VAR")
            assert result == ["sk-abc123"]

    def test_multiple_keys(self):
        from tools.search.factory import SearchProviderFactory
        with patch.dict("os.environ", {"TEST_VAR": "key1,key2,key3"}, clear=False):
            result = SearchProviderFactory._parse_keys("TEST_VAR")
            assert result == ["key1", "key2", "key3"]

    def test_whitespace_stripped(self):
        from tools.search.factory import SearchProviderFactory
        with patch.dict("os.environ", {"TEST_VAR": " key1 , key2 , "}, clear=False):
            result = SearchProviderFactory._parse_keys("TEST_VAR")
            assert result == ["key1", "key2"]

    def test_empty_entries_filtered(self):
        from tools.search.factory import SearchProviderFactory
        with patch.dict("os.environ", {"TEST_VAR": "key1,,,key2,"}, clear=False):
            result = SearchProviderFactory._parse_keys("TEST_VAR")
            assert result == ["key1", "key2"]

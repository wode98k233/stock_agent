"""LLM 缓存前缀测试"""
import pytest
from unittest.mock import patch, MagicMock
from config import Config
from utils.cache_prefix import (
    get_cache_prefix,
    should_inject_prefix,
    inject_cache_prefix,
    DEFAULT_CACHE_PREFIX,
)


class TestGetCachePrefix:
    """get_cache_prefix 测试"""

    def setup_method(self):
        # 重置缓存
        import utils.cache_prefix as m
        m._cached_prefix = None

    def test_default_prefix_no_date(self):
        """默认前缀不包含日期（避免跨天缓存失效）"""
        Config.CACHE_PREFIX_CONTENT = ""
        prefix = get_cache_prefix()
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        assert today not in prefix

    def test_custom_prefix(self):
        """自定义前缀优先于默认"""
        Config.CACHE_PREFIX_CONTENT = "自定义前缀内容"
        prefix = get_cache_prefix()
        assert prefix == "自定义前缀内容"
        Config.CACHE_PREFIX_CONTENT = ""

    def test_default_prefix_cached(self):
        """默认前缀被缓存"""
        Config.CACHE_PREFIX_CONTENT = ""
        prefix1 = get_cache_prefix()
        prefix2 = get_cache_prefix()
        assert prefix1 is prefix2  # 同一对象引用


class TestShouldInjectPrefix:
    """should_inject_prefix 测试"""

    def test_enabled(self):
        Config.CACHE_PREFIX_ENABLED = True
        assert should_inject_prefix() is True

    def test_disabled(self):
        Config.CACHE_PREFIX_ENABLED = False
        assert should_inject_prefix() is False


class TestInjectCachePrefix:
    """inject_cache_prefix 测试"""

    def setup_method(self):
        import utils.cache_prefix as m
        m._cached_prefix = None

    def test_inject_when_enabled(self):
        """启用时注入前缀"""
        Config.CACHE_PREFIX_ENABLED = True
        Config.CACHE_PREFIX_CONTENT = "测试前缀"
        messages = [("system", "原始系统提示"), ("user", "用户输入")]
        result = inject_cache_prefix(messages)
        assert len(result) == 3
        assert result[0] == ("system", "测试前缀")
        assert result[1] == ("system", "原始系统提示")
        assert result[2] == ("user", "用户输入")
        Config.CACHE_PREFIX_CONTENT = ""

    def test_skip_when_disabled(self):
        """禁用时不注入"""
        Config.CACHE_PREFIX_ENABLED = False
        messages = [("system", "原始系统提示"), ("user", "用户输入")]
        result = inject_cache_prefix(messages)
        assert len(result) == 2
        assert result == messages

    def test_skip_flag(self):
        """skip=True 时不注入"""
        Config.CACHE_PREFIX_ENABLED = True
        messages = [("system", "原始系统提示")]
        result = inject_cache_prefix(messages, skip=True)
        assert result == messages

    def test_empty_messages(self):
        """空消息列表"""
        Config.CACHE_PREFIX_ENABLED = True
        Config.CACHE_PREFIX_CONTENT = "前缀"
        result = inject_cache_prefix([])
        assert len(result) == 1
        assert result[0] == ("system", "前缀")
        Config.CACHE_PREFIX_CONTENT = ""


class TestDefaultPrefixContent:
    """默认前缀内容质量测试"""

    def test_contains_stock_market_rules(self):
        """包含 A 股市场规则"""
        assert "A 股" in DEFAULT_CACHE_PREFIX
        assert "T+1" in DEFAULT_CACHE_PREFIX
        assert "涨跌幅" in DEFAULT_CACHE_PREFIX

    def test_contains_output_spec(self):
        """包含输出规范"""
        assert "中文" in DEFAULT_CACHE_PREFIX
        assert "小数点后两位" in DEFAULT_CACHE_PREFIX

    def test_no_date_placeholder(self):
        """前缀模板不包含日期占位符（避免跨天缓存失效）"""
        assert "{date}" not in DEFAULT_CACHE_PREFIX

    def test_prefix_length(self):
        """前缀长度在合理范围内 (~300 tokens, 约 400-2000 字符)"""
        char_count = len(DEFAULT_CACHE_PREFIX)
        assert 400 < char_count < 2000, f"前缀长度 {char_count} 不在合理范围"

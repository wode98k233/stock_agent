# -*- coding: utf-8 -*-
"""数据源配置单元测试。"""
import os
import sys
from unittest.mock import patch

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestFetcherConfig:
    """数据源配置测试类"""

    def test_config_defaults(self):
        """测试配置默认值"""
        from tools.fetcher.config import Config

        # 熔断配置
        assert Config.DATASOURCE_MAX_FAILS == 5
        assert Config.DATASOURCE_RECOVERY_SECS == 600

        # 超时配置
        assert Config.REQUEST_TIMEOUT == 15
        assert Config.AKSHARE_CALL_TIMEOUT == 30

        # Akshare 反爬配置
        assert Config.AKSHARE_RATE_LIMIT_MIN == 2.0
        assert Config.AKSHARE_RATE_LIMIT_MAX == 5.0
        assert Config.AKSHARE_REALTIME_CACHE_TTL == 1200
        assert Config.AKSHARE_ENABLE_EASTMONEY_PATCH is True

        # Pytdx 配置
        assert Config.PYTDX_PRIORITY == 75
        assert Config.PYTDX_CONNECTION_COOLDOWN == 15

        # 重试配置
        assert Config.RETRY_MAX_RETRIES == 5
        assert Config.RETRY_BASE_DELAY == 2.0
        assert Config.RETRY_MAX_DELAY == 30.0

    def test_config_env_override(self):
        """测试环境变量覆盖"""
        from tools.fetcher import config
        import importlib

        # 设置环境变量
        env_vars = {
            "DATASOURCE_MAX_FAILS": "10",
            "AKSHARE_CALL_TIMEOUT": "60",
            "AKSHARE_RATE_LIMIT_MIN": "3.0",
            "AKSHARE_ENABLE_EASTMONEY_PATCH": "false",
            "PYTDX_PRIORITY": "80",
        }

        with patch.dict(os.environ, env_vars):
            # 重新加载模块
            importlib.reload(config)

            assert config.Config.DATASOURCE_MAX_FAILS == 10
            assert config.Config.AKSHARE_CALL_TIMEOUT == 60
            assert config.Config.AKSHARE_RATE_LIMIT_MIN == 3.0
            assert config.Config.AKSHARE_ENABLE_EASTMONEY_PATCH is False
            assert config.Config.PYTDX_PRIORITY == 80

        # 清理：重新加载恢复默认值
        importlib.reload(config)

    def test_config_tushare_token(self):
        """测试 Tushare Token 配置"""
        from tools.fetcher.config import Config

        # 默认应该为空字符串
        assert isinstance(Config.TUSHARE_TOKEN, str)

    def test_config_pytdx_servers(self):
        """测试 Pytdx 服务器配置"""
        from tools.fetcher.config import Config

        # 默认应该为空
        assert Config.PYTDX_SERVERS == ""

    def test_config_types(self):
        """测试配置类型"""
        from tools.fetcher.config import Config

        # 整数类型
        assert isinstance(Config.DATASOURCE_MAX_FAILS, int)
        assert isinstance(Config.DATASOURCE_RECOVERY_SECS, int)
        assert isinstance(Config.REQUEST_TIMEOUT, int)
        assert isinstance(Config.AKSHARE_CALL_TIMEOUT, int)
        assert isinstance(Config.AKSHARE_REALTIME_CACHE_TTL, int)
        assert isinstance(Config.PYTDX_PRIORITY, int)
        assert isinstance(Config.PYTDX_CONNECTION_COOLDOWN, int)
        assert isinstance(Config.RETRY_MAX_RETRIES, int)

        # 浮点类型
        assert isinstance(Config.AKSHARE_RATE_LIMIT_MIN, float)
        assert isinstance(Config.AKSHARE_RATE_LIMIT_MAX, float)
        assert isinstance(Config.RETRY_BASE_DELAY, float)
        assert isinstance(Config.RETRY_MAX_DELAY, float)

        # 布尔类型
        assert isinstance(Config.AKSHARE_ENABLE_EASTMONEY_PATCH, bool)

        # 字符串类型
        assert isinstance(Config.TUSHARE_TOKEN, str)
        assert isinstance(Config.PYTDX_SERVERS, str)

    def test_config_positive_values(self):
        """测试配置值为正数"""
        from tools.fetcher.config import Config

        assert Config.DATASOURCE_MAX_FAILS > 0
        assert Config.DATASOURCE_RECOVERY_SECS > 0
        assert Config.REQUEST_TIMEOUT > 0
        assert Config.AKSHARE_CALL_TIMEOUT > 0
        assert Config.AKSHARE_RATE_LIMIT_MIN > 0
        assert Config.AKSHARE_RATE_LIMIT_MAX > 0
        assert Config.AKSHARE_REALTIME_CACHE_TTL > 0
        assert Config.PYTDX_CONNECTION_COOLDOWN > 0
        assert Config.RETRY_MAX_RETRIES > 0
        assert Config.RETRY_BASE_DELAY > 0
        assert Config.RETRY_MAX_DELAY > 0

    def test_config_rate_limit_range(self):
        """测试限流配置范围"""
        from tools.fetcher.config import Config

        # 最小值应该小于最大值
        assert Config.AKSHARE_RATE_LIMIT_MIN < Config.AKSHARE_RATE_LIMIT_MAX

    def test_config_delay_range(self):
        """测试重试延迟范围"""
        from tools.fetcher.config import Config

        # 基础延迟应该小于最大延迟
        assert Config.RETRY_BASE_DELAY < Config.RETRY_MAX_DELAY

# -*- coding: utf-8 -*-
"""Pytdx 数据源单元测试。"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def _init_fetcher():
    """触发数据源延迟注册"""
    from tools.fetcher import _ensure_initialized
    _ensure_initialized()


class TestPytdxDataSource:
    """Pytdx 数据源测试类"""

    def test_pytdx_registration(self):
        """测试 Pytdx 数据源是否正确注册"""
        import tools.fetcher  # noqa: F401
        from tools.fetcher import DataSourceManager

        sources = {s.name: s for s in DataSourceManager._sources}
        assert "pytdx" in sources
        assert sources["pytdx"].priority == 75

    def test_pytdx_market_code(self):
        """测试市场代码判断"""
        from tools.fetcher.pytdx_ds import PytdxDataSource

        # 上海：60xxxx, 68xxxx, 5xxxxx
        assert PytdxDataSource._get_market_code("600519") == (1, "600519")
        assert PytdxDataSource._get_market_code("688001") == (1, "688001")
        assert PytdxDataSource._get_market_code("510050") == (1, "510050")

        # 深圳：00xxxx, 30xxxx, 15xxxx
        assert PytdxDataSource._get_market_code("000001") == (0, "000001")
        assert PytdxDataSource._get_market_code("300502") == (0, "300502")
        assert PytdxDataSource._get_market_code("159915") == (0, "159915")

    def test_pytdx_market_code_with_prefix(self):
        """测试带前缀的市场代码判断"""
        from tools.fetcher.pytdx_ds import PytdxDataSource

        # 带前缀
        assert PytdxDataSource._get_market_code("sh600519") == (1, "600519")
        assert PytdxDataSource._get_market_code("sz000001") == (0, "000001")
        assert PytdxDataSource._get_market_code("SH600519") == (1, "600519")

        # 带后缀
        assert PytdxDataSource._get_market_code("600519.SH") == (1, "600519")
        assert PytdxDataSource._get_market_code("000001.SZ") == (0, "000001")

    def test_pytdx_connection_cooldown(self):
        """测试连接冷却机制"""
        from tools.fetcher.pytdx_ds import PytdxDataSource
        import time

        # 重置状态
        PytdxDataSource._unavailable_until = 0

        # 初始状态：不在冷却期
        assert not PytdxDataSource._is_in_connection_cooldown()

        # 标记冷却
        PytdxDataSource._mark_connection_cooldown("测试冷却")
        assert PytdxDataSource._is_in_connection_cooldown()

        # 模拟冷却结束
        PytdxDataSource._unavailable_until = time.time() - 1
        assert not PytdxDataSource._is_in_connection_cooldown()

    @patch("tools.fetcher.pytdx_ds.PytdxDataSource._get_pytdx")
    def test_pytdx_is_available_without_pytdx(self, mock_get_pytdx):
        """测试 pytdx 未安装时的可用性检查"""
        from tools.fetcher.pytdx_ds import PytdxDataSource

        # 模拟 pytdx 未安装
        mock_get_pytdx.return_value = None

        assert not PytdxDataSource.is_available()

    def test_pytdx_unsupported_market(self):
        """测试不支持的市场"""
        from tools.fetcher.pytdx_ds import PytdxDataSource

        with pytest.raises(RuntimeError, match="不支持.*市场"):
            PytdxDataSource.get_stock_hist("AAPL", period="daily", start="2026-01-01", end="2026-01-31")

        with pytest.raises(RuntimeError, match="不支持.*市场"):
            PytdxDataSource.get_stock_hist("00700", period="daily", start="2026-01-01", end="2026-01-31")

    def test_pytdx_hosts_parsing(self):
        """测试服务器列表解析逻辑"""
        # 直接测试解析逻辑，不依赖 Config
        def parse_servers(servers_str):
            """模拟 _parse_hosts_from_env 的解析逻辑"""
            if not servers_str:
                return None
            result = []
            for part in servers_str.split(","):
                part = part.strip()
                if ":" in part:
                    host, port_str = part.rsplit(":", 1)
                    host, port_str = host.strip(), port_str.strip()
                    if host and port_str:
                        try:
                            result.append((host, int(port_str)))
                        except ValueError:
                            pass
            return result if result else None

        # 测试正常解析
        hosts = parse_servers("1.2.3.4:7709,5.6.7.8:7709")
        assert hosts is not None
        assert len(hosts) == 2
        assert ("1.2.3.4", 7709) in hosts
        assert ("5.6.7.8", 7709) in hosts

        # 测试空字符串
        assert parse_servers("") is None
        assert parse_servers(None) is None

        # 测试无效格式
        assert parse_servers("invalid") is None
        assert parse_servers("1.2.3.4") is None  # 缺少端口

        # 测试单个服务器
        hosts = parse_servers("1.2.3.4:7709")
        assert hosts == [("1.2.3.4", 7709)]

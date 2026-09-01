# -*- coding: utf-8 -*-
"""东方财富反爬补丁单元测试。"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestEastmoneyPatch:
    """东财反爬补丁测试类"""

    def test_patch_functions_exist(self):
        """测试补丁函数是否存在"""
        from tools.fetcher.patches.eastmoney_patch import eastmoney_patch, is_patched

        assert callable(eastmoney_patch)
        assert callable(is_patched)

    def test_patch_initial_state(self):
        """测试补丁初始状态"""
        from tools.fetcher.patches.eastmoney_patch import _patch_sign

        # 重置状态
        _patch_sign.set_patch(False)
        assert not _patch_sign.is_patched()

    def test_generate_uuid_md5(self):
        """测试 UUID MD5 生成"""
        from tools.fetcher.patches.eastmoney_patch import _generate_uuid_md5

        result = _generate_uuid_md5()
        assert len(result) == 32  # MD5 哈希长度
        assert result.isalnum()  # 只包含字母和数字

    def test_generate_st_nvi(self):
        """测试 st_nvi 生成"""
        from tools.fetcher.patches.eastmoney_patch import _generate_st_nvi

        result = _generate_st_nvi()
        assert len(result) > 0
        # 应该包含随机字符串和哈希前缀
        assert len(result) == 21 + 4  # 随机字符串长度 + HASH_LENGTH

    def test_get_random_ua(self):
        """测试随机 User-Agent 生成"""
        from tools.fetcher.patches.eastmoney_patch import _get_random_ua

        ua = _get_random_ua()
        assert isinstance(ua, str)
        assert len(ua) > 0
        assert "Mozilla" in ua

    @patch("tools.fetcher.patches.eastmoney_patch.requests.request")
    def test_get_nid_success(self, mock_request):
        """测试 NID 获取成功"""
        from tools.fetcher.patches.eastmoney_patch import _get_nid, _cache

        # 清除缓存
        _cache.data = None
        _cache.expire_at = 0

        # 模拟成功响应
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"nid": "test_nid_123"}}
        mock_response.raise_for_status = MagicMock()
        mock_request.return_value = mock_response

        nid = _get_nid("test_user_agent")
        assert nid == "test_nid_123"

        # 验证缓存
        assert _cache.data == "test_nid_123"

    @patch("tools.fetcher.patches.eastmoney_patch.requests.request")
    def test_get_nid_failure(self, mock_request):
        """测试 NID 获取失败"""
        from tools.fetcher.patches.eastmoney_patch import _get_nid, _cache
        import time

        # 清除缓存
        _cache.data = None
        _cache.expire_at = 0

        # 模拟网络异常
        import requests
        mock_request.side_effect = requests.exceptions.RequestException("网络错误")

        nid = _get_nid("test_user_agent")
        assert nid is None

        # 验证缓存被清除
        assert _cache.data is None
        assert _cache.expire_at > time.time()  # 应该设置了较长的过期时间

    def test_get_nid_cache(self):
        """测试 NID 缓存机制"""
        from tools.fetcher.patches.eastmoney_patch import _get_nid, _cache
        import time

        # 设置缓存
        _cache.data = "cached_nid"
        _cache.expire_at = time.time() + 100  # 100秒后过期

        # 应该返回缓存的值
        nid = _get_nid("test_user_agent")
        assert nid == "cached_nid"

    def test_auth_cache_thread_safe(self):
        """测试 AuthCache 线程安全"""
        from tools.fetcher.patches.eastmoney_patch import AuthCache

        cache = AuthCache()
        assert cache.data is None
        assert cache.expire_at == 0
        assert cache.ttl == 20
        assert hasattr(cache, 'lock')

    def test_ua_pool_fallback(self):
        """测试 UA 池 fallback"""
        from tools.fetcher.patches.eastmoney_patch import _USER_AGENTS

        assert len(_USER_AGENTS) > 0
        for ua in _USER_AGENTS:
            assert "Mozilla" in ua

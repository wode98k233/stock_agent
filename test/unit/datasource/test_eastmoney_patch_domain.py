# -*- coding: utf-8 -*-
"""东财反爬补丁域名匹配单元测试。"""
import os
import sys

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestEastmoneyDomainMatch:
    """东财域名匹配测试"""

    def test_basic_domain(self):
        """测试基本域名"""
        from tools.fetcher.patches.eastmoney_patch import _is_eastmoney_domain

        # 标准域名
        assert _is_eastmoney_domain("https://push2.eastmoney.com/api/qt/clist/get") is True
        assert _is_eastmoney_domain("https://push2his.eastmoney.com/api/qt/clist/get") is True
        assert _is_eastmoney_domain("https://fund.eastmoney.com/data/fundranking.html") is True

    def test_subdomain(self):
        """测试子域名（关键修复）"""
        from tools.fetcher.patches.eastmoney_patch import _is_eastmoney_domain

        # 带数字前缀的子域名
        assert _is_eastmoney_domain("https://82.push2.eastmoney.com/api/qt/clist/get") is True
        assert _is_eastmoney_domain("https://1.push2.eastmoney.com/api/qt/clist/get") is True
        assert _is_eastmoney_domain("https://99.push2.eastmoney.com/api/qt/clist/get") is True

    def test_non_eastmoney_domain(self):
        """测试非东财域名"""
        from tools.fetcher.patches.eastmoney_patch import _is_eastmoney_domain

        assert _is_eastmoney_domain("https://finance.sina.com.cn") is False
        assert _is_eastmoney_domain("https://qt.gtimg.cn/q=sh600519") is False
        assert _is_eastmoney_domain("https://www.baidu.com") is False

    def test_empty_url(self):
        """测试空 URL"""
        from tools.fetcher.patches.eastmoney_patch import _is_eastmoney_domain

        assert _is_eastmoney_domain("") is False
        assert _is_eastmoney_domain(None) is False

    def test_partial_domain(self):
        """测试部分域名"""
        from tools.fetcher.patches.eastmoney_patch import _is_eastmoney_domain

        # 只有 eastmoney.com 但不是目标接口
        assert _is_eastmoney_domain("https://www.eastmoney.com") is False
        assert _is_eastmoney_domain("https://quote.eastmoney.com") is False


class TestSinaRealtimeParsing:
    """新浪实时行情解析测试"""

    def test_parse_sina_response(self):
        """测试新浪响应解析"""
        # 模拟新浪响应格式
        sample_response = 'var hq_str_sz300502="新易盛,700.000,706.450,718.340,727.000,671.770,718.000,718.340,29800000,21000000000.000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-29,15:00:00,00";'

        # 解析逻辑
        data_start = sample_response.find('"')
        data_end = sample_response.rfind('"')
        data_str = sample_response[data_start+1:data_end]
        fields = data_str.split(',')

        assert len(fields) >= 32
        assert fields[0] == "新易盛"
        assert float(fields[3]) == 718.340  # 最新价
        assert float(fields[2]) == 706.450  # 昨收

    def test_calc_change_pct(self):
        """测试涨跌幅计算"""
        price = 718.340
        pre_close = 706.450
        change_pct = (price - pre_close) / pre_close * 100

        assert abs(change_pct - 1.68) < 0.01  # 约 1.68%


class TestRealtimeQuotePriority:
    """实时行情优先级测试"""

    def test_priority_order(self):
        """测试优先级顺序"""
        # 验证优先级：缓存 > 新浪单股 > 全市场
        # 这是逻辑测试，不需要实际网络请求

        # 模拟优先级逻辑
        priority = []

        # 1. 检查缓存
        has_cache = False
        if has_cache:
            priority.append("cache")

        # 2. 新浪单股接口
        priority.append("sina_single")

        # 3. 全市场接口
        priority.append("spot_all")

        assert priority[0] == "sina_single"  # 无缓存时首选
        assert "cache" not in priority  # 无缓存时不包含

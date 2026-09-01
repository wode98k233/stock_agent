"""
mx_data get_sector_fund_flow_rank sector_type 参数测试

验证：
1. 行业资金流查询包含"行业板块"
2. 概念资金流查询包含"概念板块"
3. 不同 indicator 与 sector_type 组合
4. 未知 sector_type 使用默认值
5. 错误信息包含原始查询

运行方式：
  pytest test/unit/test_mx_data_sector_type.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================
# sector_type 映射
# ============================================================

def _assert_sector_query(mock_query, expected_prefix: str):
    query = mock_query.call_args[0][0]
    assert expected_prefix in query
    assert "主力净流入" in query
    assert "涨跌幅" in query
    assert "成交额" in query

def test_industry_sector_type():
    """行业资金流查询应包含"行业板块"关键词"""
    from tools.fetcher.mx_data_ds import MXDataDataSource
    with patch.object(MXDataDataSource, '_query_mx') as mock_query:
        mock_query.return_value = MagicMock()
        MXDataDataSource.get_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        _assert_sector_query(mock_query, "今日行业板块资金流向排名")


def test_concept_sector_type():
    """概念资金流查询应包含"概念板块"关键词"""
    from tools.fetcher.mx_data_ds import MXDataDataSource
    with patch.object(MXDataDataSource, '_query_mx') as mock_query:
        mock_query.return_value = MagicMock()
        MXDataDataSource.get_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流")
        _assert_sector_query(mock_query, "今日概念板块资金流向排名")


def test_5day_indicator():
    """5日 indicator 与概念板块组合"""
    from tools.fetcher.mx_data_ds import MXDataDataSource
    with patch.object(MXDataDataSource, '_query_mx') as mock_query:
        mock_query.return_value = MagicMock()
        MXDataDataSource.get_sector_fund_flow_rank(indicator="5日", sector_type="概念资金流")
        _assert_sector_query(mock_query, "5日概念板块资金流向排名")


def test_10day_indicator():
    """10日 indicator 与行业板块组合"""
    from tools.fetcher.mx_data_ds import MXDataDataSource
    with patch.object(MXDataDataSource, '_query_mx') as mock_query:
        mock_query.return_value = MagicMock()
        MXDataDataSource.get_sector_fund_flow_rank(indicator="10日", sector_type="行业资金流")
        _assert_sector_query(mock_query, "10日行业板块资金流向排名")


# ============================================================
# 未知 sector_type
# ============================================================

def test_unknown_sector_type_defaults_to_industry():
    """未知 sector_type 应使用默认值"行业板块" """
    from tools.fetcher.mx_data_ds import MXDataDataSource
    with patch.object(MXDataDataSource, '_query_mx') as mock_query:
        mock_query.return_value = MagicMock()
        MXDataDataSource.get_sector_fund_flow_rank(indicator="今日", sector_type="未知类型")
        _assert_sector_query(mock_query, "今日行业板块资金流向排名")


def test_empty_sector_type_defaults_to_industry():
    """空字符串 sector_type 应使用默认值"行业板块" """
    from tools.fetcher.mx_data_ds import MXDataDataSource
    with patch.object(MXDataDataSource, '_query_mx') as mock_query:
        mock_query.return_value = MagicMock()
        MXDataDataSource.get_sector_fund_flow_rank(indicator="今日", sector_type="")
        _assert_sector_query(mock_query, "今日行业板块资金流向排名")


# ============================================================
# 错误信息
# ============================================================

def test_error_message_includes_query():
    """MX API 返回空数据时错误信息应包含原始查询"""
    from tools.fetcher.mx_data_ds import MXDataDataSource
    # Mock _load_mx_data 返回 mock 模块
    mock_mx_data = MagicMock()
    mock_client = MagicMock()
    mock_mx_data.MXData.return_value = mock_client
    mock_client.query.return_value = {}
    mock_mx_data.MXData.parse_result.return_value = ([], None, None, None)

    with patch.object(MXDataDataSource, '_load_mx_data', return_value=mock_mx_data):
        try:
            MXDataDataSource.get_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
            assert False, "应该抛出 RuntimeError"
        except RuntimeError as e:
            assert "今日行业板块资金流向排名" in str(e)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

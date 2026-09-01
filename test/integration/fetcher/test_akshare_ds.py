"""
akshare 相关函数集成测试
测试 tools/fetcher/__init__.py 中的统一数据获取 API
"""
import pandas as pd
import pytest

from tools.fetcher import (
    ak_stock_hist,
    ak_spot_em,
    ak_board_industry_list,
    ak_board_concept_list,
    ak_stock_news,
    ak_stock_rating,
    ak_financial_abstract,
    ak_index_daily,
)


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_stock_hist(test_stock, test_stock_name):
    """历史K线返回非空 DataFrame，包含日期/收盘等关键列"""
    df = ak_stock_hist(test_stock)

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"

    expected_cols = {"日期", "收盘"}
    actual_cols = set(df.columns)
    assert expected_cols.issubset(actual_cols), (
        f"缺少列: {expected_cols - actual_cols}，实际列: {list(df.columns)}"
    )
    assert len(df) > 0, "DataFrame 应包含至少一行数据"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_spot_em():
    """全市场实时行情返回超过 1000 条记录，包含代码列"""
    df = ak_spot_em()

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 1000, f"全市场行情应超过 1000 条，实际: {len(df)}"

    assert "代码" in df.columns, (
        f"缺少 '代码' 列，实际列: {list(df.columns)}"
    )


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_board_industry_list():
    """行业板块列表返回非空 DataFrame"""
    df = ak_board_industry_list()

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 0, "行业板块列表应包含至少一条记录"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_board_concept_list():
    """概念板块列表返回非空 DataFrame"""
    df = ak_board_concept_list()

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 0, "概念板块列表应包含至少一条记录"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_stock_news(test_stock, test_stock_name):
    """个股新闻返回非空 DataFrame"""
    df = ak_stock_news(test_stock)

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 0, "新闻列表应包含至少一条记录"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_stock_rating(test_stock, test_stock_name):
    """机构评级返回非空 DataFrame"""
    df = ak_stock_rating(test_stock)

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 0, "评级数据应包含至少一条记录"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_financial_abstract(test_stock, test_stock_name):
    """财务摘要返回非空 DataFrame"""
    df = ak_financial_abstract(test_stock)

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 0, "财务摘要应包含至少一条记录"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_ak_index_daily():
    """上证指数日线数据返回非空 DataFrame"""
    df = ak_index_daily("000001")

    assert df is not None, "返回值不应为 None"
    assert isinstance(df, pd.DataFrame), "返回值应为 DataFrame"
    assert not df.empty, "DataFrame 不应为空"
    assert len(df) > 0, "上证指数数据应包含至少一行记录"

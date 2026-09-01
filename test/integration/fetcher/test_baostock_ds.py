"""
BaoStock 数据源集成测试
测试 baostock 接口的连通性和数据结构
"""
import pandas as pd
import pytest


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_baostock_import():
    """baostock 模块可正常导入"""
    try:
        import baostock as bs
        assert hasattr(bs, "login")
        assert hasattr(bs, "query_history_k_data_plus")
    except ImportError:
        pytest.skip("baostock 未安装")


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_baostock_login_logout():
    """baostock 登录/登出正常"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        pytest.skip(f"baostock 服务器不可达: {lg.error_msg}")
    bs.logout()


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_baostock_query_hist(test_stock):
    """查询历史K线返回有效数据"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        pytest.skip(f"baostock 服务器不可达: {lg.error_msg}")
    rs = bs.query_history_k_data_plus(
        f"sh.{test_stock}",
        "date,open,high,low,close,volume",
        start_date="2026-01-01",
        end_date="2026-06-01",
        frequency="d",
        adjustflag="2",
    )
    assert rs.error_code == "0", f"查询失败: {rs.error_msg}"

    data = []
    while rs.next():
        data.append(rs.get_row_data())
    bs.logout()

    assert len(data) > 0, "应返回至少一条记录"
    # 每行应有 6 个字段
    assert len(data[0]) == 6, f"每行应有 6 个字段，实际: {len(data[0])}"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_baostock_query_realtime(test_stock):
    """查询实时行情"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        pytest.skip(f"baostock 服务器不可达: {lg.error_msg}")
    rs = bs.query_stock_basic(f"sh.{test_stock}")
    assert rs.error_code == "0"

    data = []
    while rs.next():
        data.append(rs.get_row_data())
    bs.logout()

    assert len(data) > 0, "应返回股票基本信息"

"""market_data.db 数据操作测试"""
import pandas as pd
import pytest

from utils.cache.market_data_db import (
    init_market_data_tables,
    upsert_stock_daily,
    get_stock_daily,
    get_latest_trade_date,
    get_stock_daily_count,
    upsert_stock_info,
    get_stock_info,
    fetch_and_store_daily,
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """每个测试前初始化数据库"""
    from config import Config

    db_path = tmp_path / "market_data.db"
    monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))
    init_market_data_tables()
    yield


def test_upsert_and_get_stock_daily():
    """测试插入和查询日线数据"""
    records = [
        {
            'code': '600519',
            'trade_date': '2024-01-02',
            'open': 1700.0,
            'high': 1720.0,
            'low': 1690.0,
            'close': 1710.0,
            'volume': 10000,
            'amount': 170000000.0,
        },
        {
            'code': '600519',
            'trade_date': '2024-01-03',
            'open': 1710.0,
            'high': 1730.0,
            'low': 1700.0,
            'close': 1720.0,
            'volume': 12000,
            'amount': 200000000.0,
        },
    ]

    count = upsert_stock_daily(records)
    assert count == 2

    # 查询全部
    df = get_stock_daily('600519')
    assert len(df) == 2
    assert df.iloc[0]['trade_date'] == '2024-01-02'
    assert df.iloc[1]['close'] == 1720.0

    # 带日期范围查询
    df = get_stock_daily('600519', start_date='2024-01-03')
    assert len(df) == 1
    assert df.iloc[0]['trade_date'] == '2024-01-03'


def test_upsert_stock_daily_dedup():
    """测试插入重复数据（应覆盖）"""
    record = {
        'code': '000001',
        'trade_date': '2024-01-02',
        'open': 10.0,
        'high': 11.0,
        'low': 9.0,
        'close': 10.5,
        'volume': 5000,
    }

    upsert_stock_daily([record])

    # 更新同一天的数据
    record['close'] = 10.8
    upsert_stock_daily([record])

    df = get_stock_daily('000001')
    assert len(df) == 1
    assert df.iloc[0]['close'] == 10.8


def test_get_latest_trade_date():
    """测试获取最新交易日期"""
    records = [
        {'code': '600000', 'trade_date': '2024-01-01', 'close': 10.0},
        {'code': '600000', 'trade_date': '2024-01-03', 'close': 10.5},
        {'code': '600000', 'trade_date': '2024-01-02', 'close': 10.2},
    ]
    upsert_stock_daily(records)

    latest = get_latest_trade_date('600000')
    assert latest == '2024-01-03'


def test_get_stock_daily_count():
    """测试获取数据条数"""
    records = [
        {'code': '000001', 'trade_date': f'2024-01-{i:02d}', 'close': 10.0 + i}
        for i in range(1, 11)
    ]
    upsert_stock_daily(records)

    count = get_stock_daily_count('000001')
    assert count == 10


def test_upsert_and_get_stock_info():
    """测试插入和查询股票信息"""
    info = {
        'code': '600519',
        'name': '贵州茅台',
        'market': 'SH',
        'exchange': '上交所',
        'list_date': '2001-08-27',
    }

    upsert_stock_info(info)
    result = get_stock_info('600519')

    assert result is not None
    assert result['code'] == '600519'
    assert result['name'] == '贵州茅台'
    assert result['market'] == 'SH'


def test_get_stock_info_not_found():
    """测试查询不存在的股票"""
    result = get_stock_info('999999')
    assert result is None


def test_empty_records():
    """测试空记录"""
    count = upsert_stock_daily([])
    assert count == 0

    df = get_stock_daily('999999')
    assert df.empty


def test_fetch_and_store_daily_normalizes_timestamp_index(monkeypatch):
    """同步日线数据时应将 Timestamp 索引统一为 YYYY-MM-DD。"""
    import utils.cache.market_data_db as market_data_db

    history = pd.DataFrame(
        [
            {'open': 10.0, 'high': 10.5, 'low': 9.8, 'close': 10.2, 'volume': 1000},
            {'open': 10.2, 'high': 10.8, 'low': 10.1, 'close': 10.7, 'volume': 1200},
        ],
        index=pd.to_datetime(['2024-01-02', '2024-01-03']),
    )
    monkeypatch.setattr(
        market_data_db,
        "_fetch_history_for_market_data",
        lambda code, days, source="auto", logger=None: (history, "unit"),
    )

    count = fetch_and_store_daily('600519', days=2)

    assert count == 2
    df = get_stock_daily('600519')
    assert df['trade_date'].tolist() == ['2024-01-02', '2024-01-03']


def test_fetch_and_store_daily_rejects_sparse_history(monkeypatch):
    """长周期 K 线同步不能把单条行情写成历史日线。"""
    import utils.cache.market_data_db as market_data_db

    sparse = pd.DataFrame(
        [{'open': 10.0, 'high': 10.5, 'low': 9.8, 'close': 10.2, 'volume': 1000}],
        index=pd.to_datetime(['2024-01-03']),
    )
    monkeypatch.setattr(
        market_data_db,
        "_fetch_history_for_market_data",
        lambda code, days, source="auto", logger=None: (sparse, "mx_data"),
    )

    with pytest.raises(ValueError, match="K 线过少"):
        fetch_and_store_daily('600519', days=30)

    assert get_stock_daily_count('600519') == 0

"""同花顺数据源集成测试（真连官方 REST API）

覆盖 HithinkDataSource 全接口：快照/历史K/财务/涨停池/炸板/连板/异动/龙虎榜/热股榜。
需要 HITHINK_FINANCE_API_KEY 环境变量（integration conftest 已 load_dotenv），
缺少 key 时自动 skip。

运行: python -m pytest test/integration/fetcher/test_hithink_ds.py -v -m hithink
"""
import os
import pytest
import pandas as pd

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hithink,
]


@pytest.fixture(autouse=True)
def _require_hithink_key():
    """HITHINK_FINANCE_API_KEY 未配置时跳过全部测试"""
    if not os.getenv("HITHINK_FINANCE_API_KEY"):
        pytest.skip("需要 HITHINK_FINANCE_API_KEY 环境变量")


@pytest.fixture(scope="module")
def hithink_ds():
    """获取 HithinkDataSource 类（类方法，直接引用类）"""
    from tools.fetcher.hithink_ds import HithinkDataSource
    return HithinkDataSource


# ── 行情快照 ──────────────────────────────────────────────

class TestSpot:
    """全市场快照（分页 ~60 页，慢）"""

    @pytest.mark.timeout(180)
    def test_spot_em_returns_dataframe(self, hithink_ds):
        """快照返回非空 DataFrame 且包含价格/成交量列"""
        df = hithink_ds.get_spot_em()
        assert isinstance(df, pd.DataFrame)
        assert not df.empty, "快照不应为空"
        for col in ("代码", "最新价", "成交量", "成交额"):
            assert col in df.columns, f"缺少列 {col}"


# ── 历史K线 ───────────────────────────────────────────────

class TestStockHist:
    """历史K线（单只）"""

    @pytest.mark.timeout(30)
    def test_hist_returns_dataframe(self, hithink_ds, test_stock):
        """历史K返回非空 DataFrame 且含收盘价列"""
        df = hithink_ds.get_stock_hist(test_stock, start="2026-07-01", end="2026-08-21")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty, "历史K不应为空"
        assert "日期" in df.columns and "收盘" in df.columns


# ── 财务 ──────────────────────────────────────────────────

class TestFinancial:
    """财务摘要（核心指标）"""

    @pytest.mark.timeout(30)
    def test_financial_abstract_returns(self, hithink_ds, test_stock):
        """财务摘要返回非空 DataFrame"""
        df = hithink_ds.get_financial_abstract(test_stock)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty, "财务摘要不应为空"


# ── 特色数据（涨停/炸板/连板/异动/龙虎榜/热股）────────────

class TestSpecialData:
    """特色数据接口"""

    @pytest.mark.timeout(30)
    def test_limit_up_pool(self, hithink_ds):
        """涨停池：非空 + 关键字段"""
        rows = hithink_ds.get_limit_up_pool(n=20)
        assert isinstance(rows, list) and len(rows) > 0, "涨停池不应为空"
        row = rows[0]
        for key in ("code", "name", "change_pct", "price", "consecutive_boards"):
            assert key in row, f"涨停池缺少字段 {key}"
        assert row["change_pct"] > 0, "涨停池涨跌幅应为正"

    @pytest.mark.timeout(30)
    def test_limit_break_pool(self, hithink_ds):
        """炸板池：非空 + 开板次数"""
        rows = hithink_ds.get_limit_break_pool(n=20)
        assert isinstance(rows, list)
        if rows:  # 当日可能无炸板，允许为空
            for key in ("code", "name", "open_times", "change_pct"):
                assert key in rows[0], f"炸板池缺少字段 {key}"

    @pytest.mark.timeout(30)
    def test_lianban_ladder(self, hithink_ds):
        """连板天梯：交易日 + boards 展平"""
        rows = hithink_ds.get_lianban_ladder(days=3)
        assert isinstance(rows, list) and len(rows) > 0, "连板天梯不应为空"
        assert "date" in rows[0] and "boards" in rows[0]
        if rows[0]["boards"]:
            for key in ("code", "name", "board_num"):
                assert key in rows[0]["boards"][0], f"连板梯队缺少字段 {key}"

    @pytest.mark.timeout(30)
    def test_stock_anomaly(self, hithink_ds):
        """个股异动：列表类型（当日无匹配允许为空）"""
        rows = hithink_ds.get_stock_anomaly(None, n=20, tag_codes="LIMIT_UP,SHARP_RISE")
        assert isinstance(rows, list)
        if rows:
            for key in ("code", "name", "tag_name", "analysis_content"):
                assert key in rows[0], f"异动缺少字段 {key}"

    @pytest.mark.timeout(30)
    def test_dragon_tiger_list(self, hithink_ds):
        """龙虎榜：非空 + 涨跌幅为百分数（±110 内）"""
        rows = hithink_ds.get_dragon_tiger_list(n=20, board_type="all")
        assert isinstance(rows, list) and len(rows) > 0, "龙虎榜不应为空"
        row = rows[0]
        for key in ("code", "name", "change_pct", "net_value", "buy_value", "sell_value"):
            assert key in row, f"龙虎榜缺少字段 {key}"
        assert -110 < row["change_pct"] < 110, f"涨跌幅异常: {row['change_pct']}"

    @pytest.mark.timeout(30)
    def test_hot_stocks(self, hithink_ds):
        """热股榜：非空 + 代码字段"""
        rows = hithink_ds.get_hot_stocks(n=10)
        assert isinstance(rows, list) and len(rows) > 0, "热股榜不应为空"
        assert "code" in rows[0], "热股榜缺少字段 code"

    @pytest.mark.timeout(10)
    def test_auction_snapshot_not_supported(self, hithink_ds):
        """集合竞价：官方 REST 未提供，应抛 NotImplementedError"""
        with pytest.raises(NotImplementedError):
            hithink_ds.get_auction_snapshot()

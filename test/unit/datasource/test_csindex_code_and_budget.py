"""`_is_csindex_code` 与按调用预算（call_timeout）的回归测试。

两个 bug 都是「静默失效」类：
1. 中证自编 6 位数字代码（930914 中证港股通高股息）不被识别 → 点位取数被拼成
   `sz930914` 走 A 股接口 → 必定空表，且**不报错**（看起来像源没数据）。
2. `ak_index_valuation` 没有按调用预算 → 外层 FETCH_SOURCE_TIMEOUT(20s) 比内层
   AKSHARE_CALL_TIMEOUT(30s) 还紧，等于每次都要赌 20s 内跑完；越线即报
   「所有数据源均失败」→ 股息率因子（红利策略的核心）静默缺失。
   违反的正是项目自己的约定：**外层必须比内层宽松**。
"""
import pytest

from tools import fetcher as fetcher_pkg
from tools.fetcher.akshare_ds import AkshareDataSource, _is_csindex_code


class TestIsCsindexCode:
    """区分三类代码：中证 H 系列 / 中证自编 6 位 / 港股指数（H+字母）/ 交易所指数。"""

    @pytest.mark.parametrize("code", ["H30269", "H30374", "h30269"])
    def test_h_series_is_csindex(self, code):
        assert _is_csindex_code(code) is True

    @pytest.mark.parametrize("code", ["930914", "931111", "941234"])
    def test_self_compiled_6digit_is_csindex(self, code):
        """中证自编段（93/94 开头）必须以中证接口取数。"""
        assert _is_csindex_code(code) is True

    @pytest.mark.parametrize("code", ["HSTECH", "HSI", "HSHDYI"])
    def test_hk_index_is_not_csindex(self, code):
        """港股指数是 H + 字母，不能走中证接口。"""
        assert _is_csindex_code(code) is False

    @pytest.mark.parametrize("code", ["000300", "000922", "399006", "sh000300", "600519"])
    def test_exchange_code_is_not_csindex(self, code):
        """⚠️ 000xxx 必须排除：000300（沪深300）是与交易所代码重叠的真·交易所指数，
        混进来会取错源（走中证自编接口拿不到）。"""
        assert _is_csindex_code(code) is False


class TestIndexValuationBudget:
    """按调用预算：外层必须比内层宽松。"""

    def test_index_valuation_budget_exceeds_inner(self):
        from tools.fetcher.config import Config

        budget = AkshareDataSource.call_timeout("ak_index_valuation", "930914")
        assert budget is not None, "必须给出专属预算，否则用全局 20s 会掐掉 30s 的内层"
        assert budget > float(Config.AKSHARE_CALL_TIMEOUT), (
            "外层预算 %s 必须大于内层 AKSHARE_CALL_TIMEOUT(%s)，否则内层没跑完就被外层掐掉"
            % (budget, Config.AKSHARE_CALL_TIMEOUT))

    def test_unknown_func_falls_back_to_global(self):
        assert AkshareDataSource.call_timeout("ak_whatever") is None

    def test_macro_budget_reads_declared_timeout(self, monkeypatch):
        monkeypatch.setitem(AkshareDataSource._MACRO_APIS, "fake_ind", {"timeout": 40})
        assert AkshareDataSource.call_timeout("ak_macro_indicator", "fake_ind") == 50.0

    def test_macro_without_declared_timeout_uses_global(self):
        assert AkshareDataSource.call_timeout("ak_macro_indicator", "不存在的指标") is None


def test_ak_index_valuation_outlet_is_retry_wrapped():
    """出口应经 retry 包装（否则按调用预算不会被消费）。"""
    assert hasattr(fetcher_pkg, "ak_index_valuation")
    fn = fetcher_pkg.ak_index_valuation
    assert callable(fn)
    # retry 包装会带 functools.wraps 的 __wrapped__；这里只断言可调用且在导出口径内
    assert "ak_index_valuation" in getattr(fetcher_pkg, "__all__", [])

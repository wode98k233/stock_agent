"""K 线取源的**行数校验位置**与**能力级重排**回归测试。

两个 bug 都是 2026-09-30 由「大屏策略信号长期显示数据截止 09-29」挖出来的：

1. **行数校验在源循环之外** → 部分源返回「非空但不足」的快照（mx_data 对
   000063 只给 1 行），被 `return` 短路整条优先级链，随后在循环外抛
   「K 线过少」且**不再回退**，排在其后的可用源永远轮不到。
2. **能力级重排未施加** → `order_sources(capability="hist")` 只在 `@retry()`
   路径里被调用，而本函数是手写源循环，绕过装饰器，于是按基础 priority 取源、
   `mx_data`(110) 打头。它对 hist 是表格查询、必然失败再回退，**单只白耗 ~30s**
   （实测 33s → 修复后 2.5s）。

单测只锁这两条行为，不依赖真实网络。
"""
import pandas as pd
import pytest

from utils.cache import market_data_db as mdb


class _FakeSource:
    def __init__(self, name, rows, raises=None):
        self.name = name
        self._rows = rows
        self._raises = raises
        self.calls = 0

    def get_stock_hist(self, code, period="daily", start=None, end=None):
        self.calls += 1
        if self._raises:
            raise self._raises
        if self._rows == 0:
            return pd.DataFrame()
        dates = pd.date_range("2026-01-01", periods=self._rows, freq="D")
        return pd.DataFrame({
            "date": dates, "open": [1.0] * self._rows, "high": [1.0] * self._rows,
            "low": [1.0] * self._rows, "close": [1.0] * self._rows,
            "volume": [100.0] * self._rows,
        })


@pytest.fixture()
def patch_sources(monkeypatch):
    """把源解析替换成给定的假源（按传入顺序返回）。"""
    def _apply(sources, record_order=None):
        from tools.fetcher.base import DataSourceManager

        monkeypatch.setattr(DataSourceManager, "get_available_sources",
                            classmethod(lambda cls: list(sources)))
        monkeypatch.setattr(DataSourceManager, "get_sources_for_market",
                            classmethod(lambda cls, market: list(sources)))
        monkeypatch.setattr(DataSourceManager, "detect_market",
                            classmethod(lambda cls, code: "cn"))
        if record_order is not None:
            real = DataSourceManager.order_sources

            def _spy(cls, srcs, capability):
                record_order.append(capability)
                return real(srcs, capability)
            monkeypatch.setattr(DataSourceManager, "order_sources", classmethod(_spy))
        # 关掉用户自定义顺序，确保能力级重排会生效
        monkeypatch.setattr(DataSourceManager, "_user_order", [], raising=False)
    return _apply


class TestRowCountValidationInsideLoop:
    """行数校验必须在源循环**内**，否则不足行数的源会短路整条链。"""

    def test_short_not_empty_source_falls_through_to_next(self, patch_sources):
        """用 hist 顺序里**排第一**的 hithink 造「非空但不足」，验证会继续往后试。"""
        short = _FakeSource("hithink", rows=1)     # 非空但远不足
        good = _FakeSource("akshare", rows=30)
        patch_sources([short, good])

        df, used = mdb._fetch_history_for_market_data("000063", days=40)
        assert used == "akshare", "不足行数的源必须继续往后试，而不是直接失败"
        assert len(df) == 30
        assert short.calls == 1 and good.calls == 1

    def test_mx_data_never_tried_when_ahead_source_succeeds(self, patch_sources):
        """hist 重排后 mx_data 排最后：前面源够用时它不该被调用（省一次白跑）。"""
        mx = _FakeSource("mx_data", rows=1)
        good = _FakeSource("hithink", rows=30)
        patch_sources([mx, good])                  # 传入顺序故意让 mx 在前
        df, used = mdb._fetch_history_for_market_data("000063", days=40)
        assert used == "hithink"
        assert mx.calls == 0

    def test_all_sources_short_raises_with_source_names(self, patch_sources):
        patch_sources([_FakeSource("mx_data", rows=1), _FakeSource("sina", rows=2)])
        with pytest.raises(ValueError) as ei:
            mdb._fetch_history_for_market_data("000063", days=40)
        # 错误信息要带上「哪个源给得太少」，便于定位
        assert "mx_data" in str(ei.value) and "sina" in str(ei.value)

    def test_empty_source_still_falls_through(self, patch_sources):
        patch_sources([_FakeSource("mx_data", rows=0), _FakeSource("akshare", rows=30)])
        df, used = mdb._fetch_history_for_market_data("000063", days=40)
        assert used == "akshare"

    def test_exception_source_falls_through(self, patch_sources):
        bad = _FakeSource("hithink", rows=0, raises=RuntimeError("限流"))
        good = _FakeSource("akshare", rows=30)
        patch_sources([bad, good])
        df, used = mdb._fetch_history_for_market_data("000063", days=40)
        assert used == "akshare"


class TestCapabilityReorderApplied:
    """手写源循环必须显式施加 order_sources，否则 mx_data 会排在最前。"""

    def test_order_sources_called_with_hist(self, patch_sources):
        seen = []
        patch_sources([_FakeSource("akshare", rows=30)], record_order=seen)
        mdb._fetch_history_for_market_data("000063", days=40)
        assert seen == ["hist"], "必须显式施加 hist 能力级重排"

    def test_mx_data_sinks_below_others(self, patch_sources, monkeypatch):
        """hist 能力顺序里 mx_data 排最后 → 不能用它打头（它会白跑一次）。"""
        from tools.fetcher.base import DataSourceManager
        mx = _FakeSource("mx_data", rows=1)
        ak = _FakeSource("akshare", rows=30)
        patch_sources([mx, ak])          # 传入顺序故意让 mx 在前（模拟基础 priority）
        # 让 mx 命中即成功，若重排生效则 akshare 会先被调用
        order = []
        orig = mx.get_stock_hist
        mx.get_stock_hist = lambda *a, **k: (order.append("mx"), orig(*a, **k))[1]
        orig_ak = ak.get_stock_hist
        ak.get_stock_hist = lambda *a, **k: (order.append("ak"), orig_ak(*a, **k))[1]

        df, used = mdb._fetch_history_for_market_data("000063", days=40)
        assert used == "akshare"
        assert order[0] == "ak", f"mx_data 不该先被调用（实际顺序 {order}）"

    def test_explicit_source_bypasses_list(self, patch_sources):
        """显式指定源时应只试该源（不重排、不遍历）。"""
        mx = _FakeSource("mx_data", rows=30)
        ak = _FakeSource("akshare", rows=30)
        patch_sources([mx, ak])
        df, used = mdb._fetch_history_for_market_data("000063", days=40, source="akshare")
        assert used == "akshare" and mx.calls == 0


class TestMinKlineRows:
    """`_min_kline_rows` 的边界（校验门槛本身也是契约）。"""

    @pytest.mark.parametrize("days,expected", [(1, 1), (5, 1), (20, 5), (40, 10), (60, 15)])
    def test_thresholds(self, days, expected):
        assert mdb._min_kline_rows(days) == expected

    def test_large_days_capped_at_20(self):
        assert mdb._min_kline_rows(500) == 20

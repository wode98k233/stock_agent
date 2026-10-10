"""index_daily 采集路径测试 — 指数代码分流 + 落库。

回归重点：H30269 这类「H + 纯数字」是中证指数公司代码，历史实现用
`code.startswith('H')` 粗判会把它当港股指数，送去 stock_hk_index_daily_em
必然返回空表——红利低波这类策略指数会永远采不到数据。
"""
import pandas as pd
import pytest

from utils.cache.market_data_db import (
    fetch_and_store_index_daily,
    get_index_daily,
    init_market_data_tables,
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    from config import Config
    db_path = tmp_path / "market_data.db"
    monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))
    init_market_data_tables()
    yield


@pytest.fixture(autouse=True)
def no_patch(monkeypatch):
    """跳过东财反爬补丁（真实 patch 有网络/磁盘副作用）。"""
    monkeypatch.setattr("tools.fetcher.akshare_ds.AkshareDataSource._ensure_patch",
                        classmethod(lambda cls: None))


def _csindex_df():
    """AkshareDataSource.get_index_hist_csindex 的统一输出形态。"""
    return pd.DataFrame({
        "report_date": ["2026-09-16", "2026-09-17"],
        "open": [11001.97, 10909.94],
        "high": [11032.07, 10966.20],
        "low": [10871.26, 10846.72],
        "close": [10931.94, 10890.55],
        "volume": [2.999209e9, 2.863194e9],
        "amount": [318.73, 294.85],
        "pct_change": [-0.81, -0.38],
        "change_amount": [-89.38, -41.39],
        "pe_ttm": [7.92, 7.90],
    })


class TestCsindexRouting:
    def test_h30269_uses_csindex_not_hk(self, monkeypatch):
        calls = {"csindex": 0}

        def fake_csindex(code, start="", end=""):
            calls["csindex"] += 1
            assert code == "H30269"
            assert start and len(start) == 8
            return _csindex_df()

        monkeypatch.setattr("tools.fetcher.ak_index_hist_csindex", fake_csindex)

        n = fetch_and_store_index_daily("H30269", name="中证红利低波动", days=500)

        assert n == 2
        assert calls["csindex"] == 1
        rows = get_index_daily("H30269")
        assert list(rows["close"]) == [10931.94, 10890.55]
        # 源自带涨跌幅须保留（tail 截断后重算会让首行变 NaN）
        assert list(rows["pct_change"]) == [-0.81, -0.38]

    def test_hk_index_still_routed_to_hk_api(self, monkeypatch):
        """回归守卫：修 H30269 时不得把真正的港股指数打坏——即不得走中证出口。

        2026-09-18 起港股指数改为**新浪优先、东财兜底**（东财该接口每次都要先分页拉
        spot 建代码字典，受限网络下极易被掐断，且不返回量额）。本测试相应改为：
        ① 断言不走中证出口；② 断言新浪被调用且 close 正确落库。
        """
        sin_calls = {"n": 0}
        em_calls = {"n": 0}

        def fake_sina(symbol=None):
            sin_calls["n"] += 1
            assert symbol == "HSTECH"
            return pd.DataFrame({
                "date": ["2026-09-16"], "open": [1.0], "high": [2.0],
                "low": [0.5], "close": [1.5], "volume": [100], "amount": [200],
            })

        def fake_em(symbol=None):
            em_calls["n"] += 1
            return pd.DataFrame({
                "date": ["2026-09-16"], "open": [1.0], "high": [2.0],
                "low": [0.5], "latest": [1.5],
            })

        def boom(*a, **kw):
            raise AssertionError("HSTECH 不应走中证指数出口")

        monkeypatch.setattr("tools.fetcher.ak_index_hist_csindex", boom)
        monkeypatch.setattr("akshare.stock_hk_index_daily_sina", fake_sina)
        monkeypatch.setattr("akshare.stock_hk_index_daily_em", fake_em)

        n = fetch_and_store_index_daily("HSTECH", name="恒生科技", days=500)
        assert n == 1
        assert sin_calls["n"] == 1
        assert em_calls["n"] == 0, "新浪成功时不该再打东财"

        rows = get_index_daily("HSTECH")
        assert list(rows["close"]) == [1.5], "港股指数收盘价必须落库（不能是 NULL）"

        import sqlite3
        from config import Config
        conn = sqlite3.connect(Config.get_market_data_db_path())
        name = conn.execute(
            "SELECT name FROM index_info WHERE code = 'HSTECH'").fetchone()
        conn.close()
        assert name == ("恒生科技",)

    def test_hk_index_falls_back_to_em_when_sina_fails(self, monkeypatch):
        """新浪不可用时必须仍能落库，走东财且 latest 被映射成 close。"""
        def bad_sina(symbol=None):
            raise ConnectionError("proxy closed")

        monkeypatch.setattr("akshare.stock_hk_index_daily_sina", bad_sina)
        monkeypatch.setattr("akshare.stock_hk_index_daily_em", lambda symbol=None: pd.DataFrame({
            "date": ["2026-09-15"], "open": [1.0], "high": [2.0],
            "low": [0.5], "latest": [1.8],
        }))
        monkeypatch.setattr("tools.fetcher.ak_index_hist_csindex",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                AssertionError("不应走中证指数出口")))

        n = fetch_and_store_index_daily("HSI", name="恒生指数", days=500)
        assert n == 1
        rows = get_index_daily("HSI")
        assert list(rows["close"]) == [1.8], "东财的 latest 必须映射成 close"

    def test_source_marked_csindex(self, monkeypatch):
        import sqlite3
        monkeypatch.setattr("tools.fetcher.ak_index_hist_csindex",
                            lambda code, start="", end="": _csindex_df())
        fetch_and_store_index_daily("H30269", name="中证红利低波动", days=500)

        from config import Config
        conn = sqlite3.connect(Config.get_market_data_db_path())
        src = {r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM index_daily WHERE code = 'H30269'")}
        conn.close()
        assert src == {"csindex"}

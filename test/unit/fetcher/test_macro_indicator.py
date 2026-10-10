"""宏观指标数据源测试 — mock akshare 返回值，纯本地。

覆盖：指标映射表、防御性列 rename、统一输出格式、新鲜度守卫、
fetcher 出口、retry 首调用 bug 回归（设计文档 §4.1）。
"""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


@pytest.fixture
def fake_ak(monkeypatch):
    """mock AkshareDataSource._ak，避免真实 akshare 网络调用。"""
    from tools.fetcher.akshare_ds import AkshareDataSource
    ak = MagicMock()
    monkeypatch.setattr(AkshareDataSource, "_ak", ak)
    # 跳过限流 sleep（真实 2-5s）
    monkeypatch.setattr(AkshareDataSource, "_enforce_rate_limit", classmethod(
        lambda cls, **kw: None))
    return ak


# ---------------------------------------------------------------------------
# AkshareDataSource.get_macro_indicator
# ---------------------------------------------------------------------------

class TestGetMacroIndicator:
    def test_unknown_indicator_raises(self, fake_ak):
        from tools.fetcher.akshare_ds import AkshareDataSource
        with pytest.raises(NotImplementedError, match="未知宏观指标"):
            AkshareDataSource.get_macro_indicator("nope")

    def test_m2_yoy_eastmoney_columns(self, fake_ak):
        """东财 money_supply：中文列「月份/M2 同比」→ 统一 report_date/value。"""
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.macro_china_money_supply.return_value = pd.DataFrame({
            "月份": ["2026-07", "2026-08"],
            "M2 数量": [310.0, 311.5],
            "M2 同比": [6.2, 6.1],
        })
        df = AkshareDataSource.get_macro_indicator("m2_yoy")
        assert list(df.columns) == ["report_date", "value"]
        assert df.iloc[-1]["report_date"] == "2026-08-31"  # 月度统一为月末日期
        assert df.iloc[-1]["value"] == pytest.approx(6.1)
        assert len(df) == 2

    def test_m1_m0_same_api(self, fake_ak):
        fake_ak.macro_china_money_supply.return_value = pd.DataFrame({
            "月份": ["2026-08"],
            "M1 同比": [1.5], "M0 同比": [9.0],
        })
        from tools.fetcher.akshare_ds import AkshareDataSource
        assert AkshareDataSource.get_macro_indicator("m1_yoy").iloc[0]["value"] == pytest.approx(1.5)
        assert AkshareDataSource.get_macro_indicator("m0_yoy").iloc[0]["value"] == pytest.approx(9.0)

    def test_lpr_currency_website_columns(self, fake_ak):
        """货币网 LPR：英文列 TRADE_DATE/LPR1Y。"""
        fake_ak.macro_china_lpr.return_value = pd.DataFrame({
            "TRADE_DATE": ["2026-08-20", "2026-07-20"],
            "LPR1Y": [3.0, 3.0],
            "LPR5Y": [3.5, 3.5],
        })
        from tools.fetcher.akshare_ds import AkshareDataSource
        df = AkshareDataSource.get_macro_indicator("lpr_1y")
        assert list(df.columns) == ["report_date", "value"]
        # 按日期升序
        assert df.iloc[0]["report_date"] == "2026-07-20"
        assert df.iloc[-1]["value"] == pytest.approx(3.0)

        assert AkshareDataSource.get_macro_indicator("lpr_5y").iloc[-1]["value"] == pytest.approx(3.5)

    def test_jin10_columns_cpi(self, fake_ak):
        """金十系：列「日期/今值」（数据滞后 1 年仍可入库）。"""
        fake_ak.macro_china_cpi_yearly.return_value = pd.DataFrame({
            "商品": ["全国居民消费价格指数"] * 2,
            "日期": ["2025-07-01", "2025-08-01"],
            "今值": [0.3, 0.4],
            "预测值": [0.3, 0.4],
            "前值": [0.2, 0.3],
        })
        from tools.fetcher.akshare_ds import AkshareDataSource
        df = AkshareDataSource.get_macro_indicator("cpi_yoy")
        assert df.iloc[-1]["report_date"] == "2025-08-01"
        assert df.iloc[-1]["value"] == pytest.approx(0.4)

    def test_jin10_columns_gdp_pmi(self, fake_ak):
        fake_ak.macro_china_gdp_yearly.return_value = pd.DataFrame({
            "日期": ["2025-06-01"], "今值": [5.2]})
        fake_ak.macro_china_pmi_yearly.return_value = pd.DataFrame({
            "日期": ["2025-07-01"], "今值": [49.3]})
        from tools.fetcher.akshare_ds import AkshareDataSource
        assert AkshareDataSource.get_macro_indicator("gdp_yoy").iloc[0]["value"] == pytest.approx(5.2)
        assert AkshareDataSource.get_macro_indicator("pmi_mfg").iloc[0]["value"] == pytest.approx(49.3)

    def test_empty_data_raises_runtime(self, fake_ak):
        fake_ak.macro_china_money_supply.return_value = pd.DataFrame()
        from tools.fetcher.akshare_ds import AkshareDataSource
        with pytest.raises(RuntimeError):
            AkshareDataSource.get_macro_indicator("m2_yoy")

    def test_column_missing_raises_with_hint(self, fake_ak):
        """列名漂移（缺映射列）→ 明确报错含指标名与期望列。"""
        fake_ak.macro_china_money_supply.return_value = pd.DataFrame({
            "月份": ["2026-08"], "别的列": [1.0]})  # 无 M2 同比列
        from tools.fetcher.akshare_ds import AkshareDataSource
        with pytest.raises(RuntimeError, match="m2_yoy"):
            AkshareDataSource.get_macro_indicator("m2_yoy")

    def test_base_class_stub(self):
        """基类桩：其他数据源未实现 → NotImplementedError（retry 跳过不熔断）。"""
        from tools.fetcher.base import DataSource
        with pytest.raises(NotImplementedError):
            DataSource.get_macro_indicator("m2_yoy")


# ---------------------------------------------------------------------------
# 新鲜度守卫（纯函数）
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_freshness_days_fresh(self):
        from tools.fetcher.akshare_ds import macro_freshness_days
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        assert macro_freshness_days(pd.DataFrame({"report_date": [recent]})) <= 31

    def test_freshness_days_stale(self):
        from tools.fetcher.akshare_ds import macro_freshness_days
        from datetime import date, timedelta
        stale = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
        assert macro_freshness_days(pd.DataFrame({"report_date": [stale]})) > 399

    def test_freshness_days_empty_df(self):
        from tools.fetcher.akshare_ds import macro_freshness_days
        assert macro_freshness_days(pd.DataFrame()) is None


# ---------------------------------------------------------------------------
# 国债 / 美债收益率指标（同一接口多曲线 + 必须限定回看窗口）
# ---------------------------------------------------------------------------

class TestBondYieldIndicators:
    def test_call_kwargs_include_lookback_window(self):
        """bond_zh_us_rate 无参会从 1990 拉起全量 → 必须由配置注入 start_date。"""
        from tools.fetcher.akshare_ds import AkshareDataSource, _build_macro_call_kwargs
        kw = _build_macro_call_kwargs(AkshareDataSource._MACRO_APIS["cn10_y"])
        assert "start_date" in kw
        assert len(kw["start_date"]) == 8 and kw["start_date"].isdigit()

    def test_call_kwargs_empty_for_paramless_indicators(self):
        from tools.fetcher.akshare_ds import AkshareDataSource, _build_macro_call_kwargs
        assert _build_macro_call_kwargs(AkshareDataSource._MACRO_APIS["m2_yoy"]) == {}
        assert _build_macro_call_kwargs(AkshareDataSource._MACRO_APIS["lpr_1y"]) == {}

    def test_bond_indicators_registered(self):
        from tools.fetcher.akshare_ds import AkshareDataSource
        for code in ("cn10_y", "cn30_y", "us10_y"):
            assert AkshareDataSource._MACRO_APIS[code]["period"] == "daily"

    def test_cn10_y_parses_and_passes_window(self, fake_ak):
        """东财 bond_zh_us_rate：日期列是 date 对象 → 归一为 YYYY-MM-DD。"""
        import datetime as dt
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.bond_zh_us_rate.return_value = pd.DataFrame({
            "日期": [dt.date(2026, 9, 15), dt.date(2026, 9, 16)],
            "中国国债收益率10年": [1.69, 1.6858],
            "美国国债收益率10年": [5.00, 5.01],
        })
        df = AkshareDataSource.get_macro_indicator("cn10_y")
        assert list(df.columns) == ["report_date", "value"]
        assert df.iloc[-1]["report_date"] == "2026-09-16"
        assert df.iloc[-1]["value"] == pytest.approx(1.6858)
        assert "start_date" in fake_ak.bond_zh_us_rate.call_args.kwargs

    def test_us10_y_nan_rows_dropped(self, fake_ak):
        """美债最新一日常因时差缺值 → 该行须被丢弃，不能写成 NaN 入库。"""
        import datetime as dt
        import numpy as np
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.bond_zh_us_rate.return_value = pd.DataFrame({
            "日期": [dt.date(2026, 9, 16), dt.date(2026, 9, 17)],
            "美国国债收益率10年": [5.01, np.nan],
        })
        df = AkshareDataSource.get_macro_indicator("us10_y")
        assert len(df) == 1
        assert df.iloc[-1]["report_date"] == "2026-09-16"


# ---------------------------------------------------------------------------
# 汇率 / 港元 HIBOR（2026-09-22 新增：把债基④、恒科⑤ 由手工项改为自动源）
# ---------------------------------------------------------------------------

class TestFxAndHiborIndicators:
    def test_registered_as_daily(self):
        from tools.fetcher.akshare_ds import AkshareDataSource
        for code in ("usdcny", "hk_hibor_1w"):
            assert AkshareDataSource._MACRO_APIS[code]["period"] == "daily"

    def test_usdcny_passes_symbol_and_both_dates(self):
        """currency_boc_sina 的默认区间写死在源码里（2023-03~2023-11），
        不显式传 start_date / end_date 就只会返回那段固定历史。"""
        from tools.fetcher.akshare_ds import AkshareDataSource, _build_macro_call_kwargs
        kw = _build_macro_call_kwargs(AkshareDataSource._MACRO_APIS["usdcny"])
        assert kw["symbol"] == "美元"
        assert len(kw["start_date"]) == 8 and kw["start_date"].isdigit()
        assert len(kw["end_date"]) == 8 and kw["end_date"].isdigit()
        assert kw["start_date"] < kw["end_date"]

    def test_hibor_is_paramless(self):
        from tools.fetcher.akshare_ds import AkshareDataSource, _build_macro_call_kwargs
        assert _build_macro_call_kwargs(
            AkshareDataSource._MACRO_APIS["hk_hibor_1w"]) == {}

    def test_usdcny_cent_quote_is_scaled_to_yuan(self, fake_ak):
        """中行牌价按「分」计价（6.7459 报 674.59）→ value_scale 归一到元。

        漏了这一步，台账会拿 674.59 去比对 6.85 的阈值，直接判成「贬值 −2」。
        """
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.currency_boc_sina.return_value = pd.DataFrame({
            "日期": ["2026-09-21", "2026-09-22"],
            "央行中间价": [674.87, 674.59],
        })
        df = AkshareDataSource.get_macro_indicator("usdcny")
        assert df.iloc[-1]["value"] == pytest.approx(6.7459)
        assert df.iloc[-1]["report_date"] == "2026-09-22"

    def test_hibor_reads_the_one_week_column(self, fake_ak):
        """该接口同时给 ON/1W/2W/1M/…/1Y，必须只取 1W-定价。"""
        from tools.fetcher.akshare_ds import AkshareDataSource
        fake_ak.macro_china_hk_market_info.return_value = pd.DataFrame({
            "日期": ["2026-09-18", "2026-09-21"],
            "1W-定价": [1.4091, 1.3906],
            "1M-定价": [1.5546, 1.5379],
        })
        df = AkshareDataSource.get_macro_indicator("hk_hibor_1w")
        assert list(df.columns) == ["report_date", "value"]
        assert df.iloc[-1]["value"] == pytest.approx(1.3906)
        assert df.iloc[-1]["report_date"] == "2026-09-21"


class TestDxyIndicator:
    """合成美元指数：源站给 26 列原始中间价，需本地按 ICE 官方公式换算成单一序列。"""

    def test_registered_as_calc_type(self):
        """dxy 是**本地计算型**指标，不该有 fn 键（有 fn 会去 akshare 上找不存在的属性）。"""
        from tools.fetcher.akshare_ds import AkshareDataSource, _MACRO_CALCS
        api = AkshareDataSource._MACRO_APIS["dxy"]
        assert api["period"] == "daily"
        assert "fn" not in api
        assert api["calc"] in _MACRO_CALCS

    def test_dxy_composition_uses_official_weights(self, monkeypatch):
        """用固定的中间价快照验算 DXY 绝对值，锁定 ICE 官方权重。

        为什么锚在绝对数值上：公式里任一指数写错（如 −0.576 抄成 −0.567），
        绝对值会漂零点几个点，但**趋势与方向判断看起来完全正常**——
        只测「有输出/在合理区间」抓不到这类错误。
        """
        from tools.fetcher.akshare_ds import _calc_dxy

        row = {"日期": "2026-09-22", "美元": 700.00, "欧元": 800.00, "日元": 4.50,
               "英镑": 900.00, "加元": 500.00, "瑞典克朗": 140.00, "瑞士法郎": 800.00}
        html = ("<table><tr>" + "".join("<th>%s</th>" % k for k in row)
                + "</tr><tr>" + "".join("<td>%s</td>" % v for v in row.values())
                + "</tr></table>")

        class _Resp:
            text = html

        class _Sess:
            def __init__(self, *a, **kw):
                pass

            def post(self, *a, **kw):
                return _Resp()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("requests.Session", _Sess)
        df = _calc_dxy()

        # 比值与 /100 无关：全部按「每 100 外币兑人民币」报价，日元口径本就如此
        eurusd, usdjpy, gbpusd = 800 / 700, 700 / 4.50, 900 / 700
        usdcad, usdsek, usdchf = 700 / 500, 700 / 140, 700 / 800
        expect = (50.14348112 * eurusd ** -0.576 * usdjpy ** 0.136 * gbpusd ** -0.119
                  * usdcad ** 0.091 * usdsek ** 0.042 * usdchf ** 0.036)

        assert len(df) == 1
        assert df.iloc[0]["report_date"] == "2026-09-22"
        assert float(df.iloc[0]["value"]) == pytest.approx(expect, abs=0.01)

    def test_dxy_uses_verify_false_for_safe_gov_cn(self, monkeypatch):
        """外管局站点在本机代理链下过不了标准校验——这正是台账早期 note 所记
        「外管局 SSL 拒绝」的真身，故必须显式关闭校验（否则整条取数链路直接抛错）。"""
        from tools.fetcher.akshare_ds import _calc_dxy

        seen = {}

        class _Resp:
            # 必须给全 6 种货币：缺列会先抛 KeyError，就测不到 verify 了
            text = ("<table><tr><th>日期</th><th>美元</th><th>欧元</th><th>日元</th>"
                    "<th>英镑</th><th>加元</th><th>瑞典克朗</th><th>瑞士法郎</th></tr>"
                    "<tr><td>2026-09-22</td><td>700.00</td><td>800.00</td><td>4.50</td>"
                    "<td>900.00</td><td>500.00</td><td>140.00</td><td>800.00</td></tr>"
                    "</table>")

        class _Sess:
            def __init__(self, *a, **kw):
                pass

            def post(self, *a, **kw):
                seen["verify"] = self.verify
                return _Resp()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("requests.Session", _Sess)
        _calc_dxy()
        assert seen["verify"] is False


# ---------------------------------------------------------------------------
# fetcher 出口 + retry 首调用 bug 回归
# ---------------------------------------------------------------------------

class TestFetcherExport:
    def test_ak_macro_indicator_export(self, monkeypatch):
        from tools.fetcher import ak_macro_indicator
        fake_df = pd.DataFrame({"report_date": ["2026-08-31"], "value": [6.1]})
        monkeypatch.setattr(
            "tools.fetcher.base.DataSourceManager.get_available_sources",
            classmethod(lambda cls, capability=None: [
                type("S", (), {"name": "fake", "priority": 100,
                               "is_available": staticmethod(lambda: True),
                               "get_macro_indicator": classmethod(
                                   lambda cls, indicator: fake_df),
                               "_enforce_rate_limit": classmethod(lambda cls, **kw: None)})]))
        df = ak_macro_indicator("m2_yoy")
        assert list(df.columns) == ["report_date", "value"]
        assert df.iloc[0]["value"] == pytest.approx(6.1)


class TestRetryFirstCallBug:
    """回归：进程内首个 fetcher API 调用必须成功（可用源为空的必现 bug）。

    根因：retry() wrapper 在调 func 前读 DataSourceManager._sources，
    而数据源注册发生在 func 内的 _ensure_initialized()——首次调用时
    _sources 为空 → 直接 raise「没有可用的数据源」。

    隔离方式：接管 _ensure_initialized，让它只注册一个假源。
    **不要**改成「mock 掉 akshare 的 get_margin_trading」——retry 按优先级挑选
    数据源，mx_data/hithink 等优先级高于 akshare，会绕过 mock 真发网络请求
    （历史症状：断言 1 行、实际拿到 2 行真实融资融券数据）。
    """

    def test_first_call_succeeds_without_manual_init(self, monkeypatch):
        import tools.fetcher as pkg
        from tools.fetcher.base import DataSource, DataSourceManager

        fake_df = pd.DataFrame({"date": ["2026-09-01"]})

        class FakeFirstCallSource(DataSource):
            name = "fake_first_call"
            priority = 100

            @classmethod
            def is_available(cls):
                return True

            @classmethod
            def _enforce_rate_limit(cls, *a, **kw):
                return None

            @classmethod
            def get_margin_trading(cls, market="sh", start_date="", end_date=""):
                return fake_df

        def fake_ensure_initialized():
            """惰性初始化：只注册假源，杜绝真实网络与优先级干扰。"""
            monkeypatch.setattr(pkg, "_initialized", True)
            DataSourceManager.register_source(FakeFirstCallSource)

        # 模拟进程初始状态：未初始化 + 无已注册源
        monkeypatch.setattr(pkg, "_initialized", False)
        monkeypatch.setattr(DataSourceManager, "_sources", [])
        monkeypatch.setattr(pkg, "_ensure_initialized", fake_ensure_initialized)

        df = pkg.ak_margin_trading()  # 修复前：RuntimeError 没有可用的数据源

        assert df is not None and len(df) == 1
        # 惰性初始化确实被 wrapper 触发（注册发生在调用之后）
        assert [s.name for s in DataSourceManager._sources] == ["fake_first_call"]

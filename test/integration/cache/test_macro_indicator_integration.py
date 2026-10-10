"""宏观指标真实接口集成测试 — 逐指标实测列名与新鲜度（§2 实证复验）。

真实 akshare 调用（限流 2-5s/次 × 8 指标 ≈ 30-60s），标 integration。
金十系（cpi/gdp/pmi）滞后约 1 年为已知事实，断言其滞后天数 > 300
验证「源数据滞后」标注逻辑；东财/货币系断言新鲜（< 90 天）。
"""
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(300),
]

ALL_INDICATORS = ["m2_yoy", "m1_yoy", "m0_yoy", "lpr_1y", "lpr_5y",
                  "cpi_yoy", "gdp_yoy", "pmi_mfg"]


@pytest.mark.parametrize("indicator", ALL_INDICATORS)
def test_fetch_and_columns(indicator):
    """每指标真实拉取：统一列 report_date/value、非空、日期升序。"""
    from tools.fetcher import ak_macro_indicator
    df = ak_macro_indicator(indicator)
    assert list(df.columns) == ["report_date", "value"]
    assert len(df) >= 12  # 至少一年月度
    assert df["report_date"].is_monotonic_increasing
    assert df["value"].notna().all()


@pytest.mark.parametrize("indicator,expect_fresh", [
    ("m2_yoy", True), ("m1_yoy", True), ("m0_yoy", True),
    ("lpr_1y", True), ("lpr_5y", True),
    ("cpi_yoy", False), ("gdp_yoy", False), ("pmi_mfg", False),
])
def test_freshness(indicator, expect_fresh):
    """新鲜度复验：东财/货币系 < 90 天；金十系滞后 > 300 天（源数据滞后标注依据）。"""
    from tools.fetcher import ak_macro_indicator
    from tools.fetcher.akshare_ds import macro_freshness_days
    df = ak_macro_indicator(indicator)
    days = macro_freshness_days(df)
    assert days is not None
    if expect_fresh:
        assert days < 90, f"{indicator} 源已滞后 {days} 天（预期新鲜）"
    else:
        assert days > 300, f"{indicator} 意外新鲜（{days} 天）——请更新设计文档滞后标注"


def test_full_sync_to_db(tmp_path):
    """全量同步落库：8 指标全部入库 + 幂等重跑 added=0。"""
    from unittest.mock import MagicMock, patch
    from utils.cache import fundamental_db
    from utils.cache.macro_db import fetch_and_store_macro, get_macro_series, \
        MACRO_INDICATOR_META

    db_path = str(tmp_path / "fundamental_cache.db")
    mock_config = MagicMock()
    mock_config.get_fundamental_db_path.return_value = db_path
    with patch.object(fundamental_db, "Config", mock_config):
        from utils.cache.macro_db import init_macro_tables
        init_macro_tables()
        result = fetch_and_store_macro(list(MACRO_INDICATOR_META))
        errors = {k: v for k, v in result.items() if "error" in v}
        assert not errors, f"同步失败: {errors}"
        for code in MACRO_INDICATOR_META:
            assert get_macro_series(code, periods=3), code

        # 幂等：立即重跑全量 → 全部 added=0
        result2 = fetch_and_store_macro(list(MACRO_INDICATOR_META))
        assert all("error" not in v and v["added"] == 0 for v in result2.values())

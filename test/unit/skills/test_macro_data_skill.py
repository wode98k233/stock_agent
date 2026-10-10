"""macro_data 技能测试 — 查库优先 / miss 拉取落库 / 滞后标注。

tools/skills 非标准包（SkillRegister 按文件动态加载），测试用
importlib spec_from_file_location 同款方式加载 main.py。
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_MAIN_PY = Path(__file__).resolve().parents[3] / "tools" / "skills" / "macro_data" / "main.py"


def _load_skill_module():
    spec = importlib.util.spec_from_file_location("macro_data_main", _MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def skill_db(tmp_path):
    """临时 fundamental_cache.db（技能查库路径）。"""
    from utils.cache import fundamental_db, macro_db as mdb
    db_path = str(tmp_path / "fundamental_cache.db")
    mock_config = MagicMock()
    mock_config.get_fundamental_db_path.return_value = db_path
    # patch 住模块引用的 Config（fundamental_db 的闭包与 macro_db 内部引用）
    with patch.object(fundamental_db, "Config", mock_config), \
         patch.object(mdb, "get_fundamental_db", fundamental_db.get_fundamental_db):
        mdb.init_macro_tables()
        yield mdb


def _make_skill(mdb):
    MacroDataSkill = _load_skill_module().MacroDataSkill
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    s = MacroDataSkill(logger)
    s._macro_db = mdb  # 注入临时库（生产为 utils.cache.macro_db）
    return s


def _call(skill, indicator, **kw):
    """调用工具并解包 JSON（@skill_tool 自动序列化返回值）。"""
    import json
    return json.loads(skill.get_macro_series(indicator, **kw))


class TestGetMacroSeries:
    def test_hit_db_no_fetch(self, skill_db):
        """库里有数据 → 直接返回，不触发网络拉取。"""
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": ["2026-07-31", "2026-08-31"], "value": [7.7, 7.6]}))
        skill_db.fetch_and_store_macro(["m2_yoy"], fetcher=fetcher)

        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", MagicMock()) as spy_fetch:
            result = _call(skill, "m2_yoy", periods=12)
        spy_fetch.assert_not_called()
        assert result["indicator"] == "m2_yoy"
        assert result["label"] == "M2 同比"
        assert result["unit"] == "%"
        assert result["latest"] == pytest.approx(7.6)
        assert result["latest_date"] == "2026-08-31"
        assert result["prev"] == pytest.approx(7.7)
        assert result["change"] == pytest.approx(-0.1)
        assert len(result["series"]) == 2

    def test_miss_fetches_and_stores(self, skill_db):
        """库空 → 拉取 + 落库 + 返回（下轮走库）。"""
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": ["2026-07-31", "2026-08-31"], "value": [7.7, 7.6]}))
        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", fetcher):
            result = _call(skill, "m2_yoy")

        assert result["latest"] == pytest.approx(7.6)
        # 已落库：再查走库
        assert len(skill_db.get_macro_series("m2_yoy")) == 2
        with patch.object(skill, "_fetch_indicator", MagicMock()) as spy:
            skill.get_macro_series("m2_yoy")
        spy.assert_not_called()

    def test_stale_flag_for_jin10(self, skill_db):
        """金十系滞后数据 → stale=True + stale_days 标注。"""
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=500)).strftime("%Y-%m-%d")
        older = (date.today() - timedelta(days=530)).strftime("%Y-%m-%d")
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": [older, old], "value": [0.3, 0.4]}))
        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", fetcher):
            result = _call(skill, "cpi_yoy")
        assert result["stale"] is True
        assert result["stale_days"] > 400

    def test_fresh_no_stale_flag(self, skill_db):
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": ["2026-07-31", "2026-08-31"], "value": [7.7, 7.6]}))
        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", fetcher):
            result = _call(skill, "m2_yoy")
        assert result["stale"] is False

    def test_daily_indicator_stale_threshold_is_tight(self, skill_db):
        """日频指标（10Y 国债）滞后 30 天就该标 stale——统一 400 天会漏报。"""
        from datetime import date, timedelta
        old_d = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        older = (date.today() - timedelta(days=31)).strftime("%Y-%m-%d")
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": [older, old_d], "value": [1.69, 1.68]}))
        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", fetcher):
            result = _call(skill, "cn10_y")
        assert result["period_type"] == "daily"
        assert result["stale"] is True
        assert result["stale_days"] == 30

    def test_daily_indicator_fresh_when_recent(self, skill_db):
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        fetcher = MagicMock(return_value=pd.DataFrame({
            "report_date": [recent], "value": [1.6858]}))
        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", fetcher):
            result = _call(skill, "cn10_y")
        assert result["stale"] is False

    def test_unknown_indicator_error(self, skill_db):
        skill = _make_skill(skill_db)
        result = _call(skill, "nope")
        assert "error" in result or "可用指标" in str(result)

    def test_available_indicators_listed(self, skill_db):
        """未知指标报错时应列出全部可用指标（引导 LLM 自纠）。"""
        skill = _make_skill(skill_db)
        result = _call(skill, "nope")
        assert "m2_yoy" in str(result) and "lpr_1y" in str(result)

    def test_fetch_failure_returns_error_not_raise(self, skill_db):
        """拉取失败 → 返回 error dict 不抛（agent 工具层容错）。"""
        def boom(indicator):
            raise RuntimeError("接口挂了")
        skill = _make_skill(skill_db)
        with patch.object(skill, "_fetch_indicator", boom):
            result = _call(skill, "m2_yoy")
        assert "error" in result


class TestSkillMetadata:
    def test_build_tools_registered(self):
        """build_tools 出口返回 LangChain 工具（skill_register 兼容）。"""
        build_tools = _load_skill_module().build_tools
        logger = MagicMock()
        tools = build_tools(logger, None)
        assert len(tools) >= 1
        assert any("macro" in (t.name or "").lower() for t in tools)

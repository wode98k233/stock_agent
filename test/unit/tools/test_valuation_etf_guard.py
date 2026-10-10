"""ETF 与个股的标的类型分流（`get_valuation_percentile` 的 ETF 闸门）。

2026-09-22 实测：对 159570（港股通创新药 ETF）调用 `get_valuation_percentile`，
底层 mx_data 因 ETF 无 PE/PB 返回「接口返回中无 dataTableDTOList」→ 工具报 error →
污染工具失败率，且报告据此写「数据缺失」。ETF 与个股不同，**没有 PE(TTM)/PB 口径**。

个股路径（含成功/失败）已由 test/unit/skills/test_stock_analysis_backup_tools.py 覆盖，
本文件只补 ETF 分流与 `is_etf_code` 的边界。
"""
import importlib.util
import json
import logging
from pathlib import Path

import pytest

from tools.valuation import is_etf_code

pytestmark = pytest.mark.unit


def _load_skill_module():
    """按文件路径加载技能 main.py（tools/skills.py 与 tools/skills/ 同名，不能直接 import）。"""
    path = Path(__file__).resolve().parents[3] / "tools" / "skills" / "stock_analysis" / "main.py"
    spec = importlib.util.spec_from_file_location("stock_analysis_etf_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestIsEtfCode:
    @pytest.mark.parametrize("code", [
        "159570", "159570.SZ", "sz159570", "SZ159570",
        "510300", "510300.SH", "513330", "sh513330", "588000", "561000",
        "530001", "551001",
    ])
    def test_etf_codes(self, code):
        assert is_etf_code(code) is True

    @pytest.mark.parametrize("code", [
        "603369", "600519", "000001", "300750", "688235", "002415",   # 个股
        "161725",   # LOF（16 开头，不得误判）
        "180101",   # REITs/封基（18 开头，不得误判）
        "123456",   # 可转债
        "00700", "00700.HK",   # 港股 5 位
        "AAPL",     # 美股
        "", "abc", "12345",
    ])
    def test_non_etf_codes(self, code):
        assert is_etf_code(code) is False


class TestValuationToolRejectsEtf:
    def test_etf_returns_not_applicable_without_error(self):
        """ETF 必须返回「不适用」，且**不得**带 error 键（免得被当成工具失败）。"""
        skill = _load_skill_module().StockAnalysisSkill(logging.getLogger("test-etf-guard"))
        res = json.loads(skill.get_valuation_percentile("159570"))
        assert res["applicable"] is False
        assert "error" not in res
        assert "指数" in res["hint"] or "指数" in res["note"]

    def test_etf_guard_runs_before_any_data_fetch(self, monkeypatch):
        """闸门必须在取数之前 —— 否则仍会打网络/命中缓存。"""
        import tools.fetcher as fetcher

        def _boom(*a, **k):
            raise AssertionError("ETF 分支不应触发任何取数")

        monkeypatch.setattr(fetcher, "ak_valuation_history", _boom)
        skill = _load_skill_module().StockAnalysisSkill(logging.getLogger("test-etf-guard2"))
        res = json.loads(skill.get_valuation_percentile("513330"))
        assert res["applicable"] is False

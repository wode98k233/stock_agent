"""
Test: 5个新Skill集成测试（真实API调用）
验证: money_flow, margin_trading, sector_rotation, risk_metrics, block_trades
"""
import importlib.util
import json
import logging
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

logger = logging.getLogger('test_integration')


def _load_skill_module(skill_name, class_name):
    """动态加载 skill 模块（绕过 tools.skills 包名冲突）"""
    skill_path = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "tools", "skills", skill_name, "main.py"))
    spec = importlib.util.spec_from_file_location(f"skill_{skill_name}", skill_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name)


def _parse(result):
    """skill_tool 装饰器返回 JSON 字符串，解析为 Python 对象"""
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


# ── money_flow ──────────────────────────────────────────────

def test_money_flow_stock_fund_flow():
    """个股资金流向"""
    MoneyFlowSkill = _load_skill_module("money_flow", "MoneyFlowSkill")
    skill = MoneyFlowSkill(logger, None)
    result = _parse(skill.get_stock_fund_flow("600519"))
    assert isinstance(result, dict)
    assert 'error' not in result or result.get('error') is None


def test_money_flow_sector_fund_flow():
    """板块资金流向"""
    MoneyFlowSkill = _load_skill_module("money_flow", "MoneyFlowSkill")
    skill = MoneyFlowSkill(logger, None)
    result = _parse(skill.get_sector_fund_flow("今日", "行业资金流"))
    assert isinstance(result, list)


def test_money_flow_north_fund():
    """北向资金"""
    MoneyFlowSkill = _load_skill_module("money_flow", "MoneyFlowSkill")
    skill = MoneyFlowSkill(logger, None)
    result = _parse(skill.get_north_fund_flow("北向资金"))
    assert isinstance(result, dict)


# ── margin_trading ──────────────────────────────────────────

def test_margin_trading_summary():
    """两融余额汇总"""
    MarginTradingSkill = _load_skill_module("margin_trading", "MarginTradingSkill")
    skill = MarginTradingSkill(logger, None)
    result = _parse(skill.get_margin_summary("sh", 10))
    assert isinstance(result, dict)


def test_margin_trading_detail():
    """两融个股明细"""
    MarginTradingSkill = _load_skill_module("margin_trading", "MarginTradingSkill")
    skill = MarginTradingSkill(logger, None)
    result = _parse(skill.get_margin_detail("sh"))
    assert isinstance(result, list)


# ── sector_rotation ─────────────────────────────────────────

def test_sector_rotation_ranking():
    """板块行情排名"""
    SectorRotationSkill = _load_skill_module("sector_rotation", "SectorRotationSkill")
    skill = SectorRotationSkill(logger, None)
    result = _parse(skill.get_sector_ranking("行业板块"))
    assert isinstance(result, list)


def test_sector_rotation_history():
    """板块历史K线"""
    SectorRotationSkill = _load_skill_module("sector_rotation", "SectorRotationSkill")
    skill = SectorRotationSkill(logger, None)
    result = _parse(skill.get_sector_history("电力", 30))
    assert isinstance(result, dict)


def test_sector_rotation_fund_flow():
    """板块资金流向"""
    SectorRotationSkill = _load_skill_module("sector_rotation", "SectorRotationSkill")
    skill = SectorRotationSkill(logger, None)
    result = _parse(skill.get_sector_fund_flow("今日", "行业资金流"))
    assert isinstance(result, list)


# ── risk_metrics ────────────────────────────────────────────

def test_risk_metrics():
    """风险指标计算"""
    RiskMetricsSkill = _load_skill_module("risk_metrics", "RiskMetricsSkill")
    skill = RiskMetricsSkill(logger, None)
    result = _parse(skill.get_risk_metrics("600519", 120, "000300"))
    assert isinstance(result, dict)


# ── block_trades ────────────────────────────────────────────

def test_block_trades_detail():
    """大宗交易明细"""
    BlockTradesSkill = _load_skill_module("block_trades", "BlockTradesSkill")
    skill = BlockTradesSkill(logger, None)
    result = _parse(skill.get_block_trade_detail("A股", 7))
    assert isinstance(result, list)


def test_block_trades_stats():
    """大宗交易统计"""
    BlockTradesSkill = _load_skill_module("block_trades", "BlockTradesSkill")
    skill = BlockTradesSkill(logger, None)
    result = _parse(skill.get_block_trade_stats(10))
    assert isinstance(result, list)
